import json
import logging
import random
import re
import os
import emojis
from flask import Flask, abort, render_template, request, jsonify, redirect, url_for, flash, session
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from flask_socketio import SocketIO, emit, join_room, leave_room
import google.generativeai as genai
from dotenv import load_dotenv
from datetime import datetime, timedelta
from sqlalchemy import func, or_, case

from models import Comment, Follow, Like, Message, Notification, User, Post, db

load_dotenv()
app = Flask(__name__)

# Configure your database URI
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///social_media.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.urandom(24).hex()
app.config['UPLOAD_FOLDER'] = 'static/uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max-limit
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)  # Set session to last for 30 days

db.init_app(app)

from cache_utils import cache


cache.init_app(app)

socketio = SocketIO(app, message_queue='memory://')

genai.configure(api_key=os.getenv('GEMINI_API_KEY'))
model = genai.GenerativeModel('gemini-pro')



migrate = Migrate(app, db)

# Import and register blueprints here
from prof import profile as profile_blueprint  # Import your blueprint
app.register_blueprint(profile_blueprint)


def is_valid_input(text):
    return text and len(text.strip()) > 0

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in {'png', 'jpg', 'jpeg', 'gif', 'mp4'}

from flask_login import current_user, login_required

@app.route('/')
def index():
    if current_user.is_authenticated:
        all_users = User.query.all()
        all_posts = Post.query.order_by(Post.timestamp.desc()).all()
        
        # Add comment counts to each post
        for post in all_posts:
            post.comment_count = post.comments.count()
    else:
        all_users = []
        all_posts = []

    return render_template('index.html', all_users=all_users, all_posts=all_posts)


login_manager = LoginManager(app)
login_manager.login_view = 'login'

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

@app.before_request
def make_session_permanent():
    session.permanent = True
    app.permanent_session_lifetime = timedelta(days=30)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form['username']).first()
        if user and check_password_hash(user.password_hash, request.form['password']):
            login_user(user, remember=True)  # Set remember=True to keep the user logged in
            session['user_id'] = user.id  # Store user_id in session
            next_page = request.args.get('next')
            return redirect(next_page or url_for('index'))
        flash('Invalid username or password')
    return render_template('login.html')


@app.route('/logout')

def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        hashed_password = generate_password_hash(request.form['password'])
        new_user = User(username=request.form['username'], email=request.form['email'], password_hash=hashed_password)
        db.session.add(new_user)
        db.session.commit()
        flash('Account created successfully')
        return redirect(url_for('login'))
    return render_template('register.html')

@app.template_filter('replace_usernames')
def replace_usernames(text):
    def replace_username(match):
        username = match.group(1)
        return f'<a href="{url_for("profile", username=username)}" class="text-blue-500 hover:underline">@{username}</a>'
    
    return re.sub(r'@(\w+)', replace_username, text)

@app.route('/api/user_activity/<int:user_id>')
@login_required
def user_activity(user_id):
    user = User.query.get_or_404(user_id)
    
    # Get posts count for the last 30 days
    thirty_days_ago = datetime.utcnow() - timedelta(days=30)
    post_counts = db.session.query(
        func.date(Post.timestamp).label('date'),
        func.count(Post.id).label('count')
    ).filter(
        Post.user_id == user_id,
        Post.timestamp >= thirty_days_ago
    ).group_by(func.date(Post.timestamp)).all()
    
    dates = [(thirty_days_ago + timedelta(days=i)).strftime('%Y-%m-%d') for i in range(31)]
    counts = [0] * 31
    
    for date, count in post_counts:
        index = (date.date() - thirty_days_ago.date()).days
        counts[index] = count
    
    return jsonify({
        'labels': dates,
        'posts': counts
    })



@app.route('/profile/<username>')
@login_required
def user_profile(username):
    user = User.query.filter_by(username=username).first_or_404()
    posts = Post.query.filter_by(user_id=user.id).all()
    followers_count = user.followers.count()  # Assuming you have a way to get the count of followers
    return render_template('profile.html', user=user, posts=posts, followers_count=followers_count)


@app.route('/edit_profile', methods=['GET', 'POST'])
@login_required
def edit_profile():
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email')
        bio = request.form.get('bio')
        location = request.form.get('location')

        if username:
            current_user.username = username
        if email:
            current_user.email = email
        if bio:
            current_user.bio = bio
        if location:
            current_user.location = location

        if 'profile_picture' in request.files:
            file = request.files['profile_picture']
            if file and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                file.save(os.path.join('static/uploads', filename))
                current_user.profile_picture = filename

        db.session.commit()
        flash('Your profile has been updated.', 'success')
        return redirect(url_for('prof.user_profile', username=current_user.username))

    return render_template('edit_profile.html', user=current_user)

@app.route('/api/follow/<username>', methods=['POST'])
@login_required
def follow(username):
    user = User.query.filter_by(username=username).first()
    if user is None:
        return jsonify({'error': 'User not found.'}), 404
    if user == current_user:
        return jsonify({'error': 'You cannot follow yourself!'}), 400
    
    if not current_user.is_following(user):
        current_user.follow(user)
        return jsonify({
            'message': f'You are now following {username}!',
            'followerCount': user.followers.count()
        })
    else:
        return jsonify({'error': 'You are already following this user.'}), 400

@app.route('/api/unfollow/<username>', methods=['POST'])
@login_required
def unfollow(username):
    user = User.query.filter_by(username=username).first()
    if user is None:
        return jsonify({'error': 'User not found.'}), 404
    if user == current_user:
        return jsonify({'error': 'You cannot unfollow yourself!'}), 400
    
    if current_user.is_following(user):
        current_user.unfollow(user)
        return jsonify({
            'message': f'You have unfollowed {username}.',
            'followerCount': user.followers.count()
        })
    else:
        return jsonify({'error': 'You are not following this user.'}), 400

@app.route('/api/delete_account', methods=['POST'])
@login_required
def delete_account():
    try:

        db.session.delete(current_user)
        db.session.commit()
        flash('Your account has been successfully deleted.')
        return jsonify({'message': 'Account deleted successfully', 'redirect': url_for('register')})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': 'An error occurred while deleting your account.'}), 500


logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

from flask import render_template, flash, redirect, url_for

@app.route('/post', methods=['POST'])
@login_required
def post():
    user_input = request.form.get('content', '').strip()
    if not user_input:
        flash('Post content cannot be empty!', 'error')
        return redirect(url_for('index'))

    # Use Gemini model to check for community guideline violations
    prompt = f"""
    Analyze the following text for any violations of community guidelines. 
    If violations are found, provide a friendly explanation and suggest 3 alternative wordings.
    Make the suggestions fun and engaging.
    Text to analyze: "{user_input}"
    
    Respond in the following JSON format:
    {{
        "violates_guidelines": boolean,
        "explanation": "string",
        "suggestions": ["string"]
    }}
    """
    try:
        response = model.generate_content(prompt)
        logger.debug(f"Gemini response text: {response.text}")
        
        # Extract JSON from the response
        json_match = re.search(r'\{.*\}', response.text, re.DOTALL)
        if json_match:
            response_json = json_match.group(0)
            try:
                response_data = json.loads(response_json)
            except json.JSONDecodeError as e:
                logger.error(f"JSONDecodeError: {str(e)} - Response: {response_json}")
                flash('An error occurred while processing your post. Please try again.', 'error')
                return redirect(url_for('index'))
        else:
            raise ValueError("No valid JSON found in the response")
        
        logger.debug(f"Parsed response data: {response_data}")

        if response_data.get('violates_guidelines', False):
            flash(response_data.get('explanation', 'Your post may violate community guidelines.'), 'warning')
            return render_template('index.html', 
                                   violation_suggestions=response_data.get('suggestions', []),
                                   original_content=user_input)
        
        # If no violations, create the post
        new_post = Post(content=user_input, user_id=current_user.id, timestamp=datetime.utcnow())
        
        if 'media' in request.files:
            file = request.files['media']
            if file and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                file.save(filepath)
                new_post.media_url = filename
        
        db.session.add(new_post)
        db.session.commit()
        
        flash('Your post has been created successfully!', 'success')
        return redirect(url_for('index'))

    except Exception as e:
        logger.error(f"Error processing or saving post: {str(e)}", exc_info=True)
        flash('An error occurred while processing your post. Please try again.', 'error')
        return redirect(url_for('index'))
@app.route('/submit_post', methods=['POST'])
@login_required
def submit_post():
    content = request.form.get('content', '').strip()
    if not content:
        return jsonify({'error': 'Post content cannot be empty!'}), 400

    
    prompt = f"""
    Analyze the following text for any violations of community guidelines. 
    If violations are found, provide a friendly explanation and suggest 3 alternative wordings.
    Make the suggestions fun and engaging.
    Text to analyze: "{content}"
    
    Respond in the following JSON format:
    {{
        "violates_guidelines": boolean,
        "explanation": "string",
        "suggestions": ["string"]
    }}
    """
    try:
        response = model.generate_content(prompt)
        logger.debug(f"Gemini response text: {response.text}")
        
        json_match = re.search(r'\{.*\}', response.text, re.DOTALL)
        if json_match:
            response_data = json.loads(json_match.group(0))
        else:
            raise ValueError("No valid JSON found in the response")
        
        logger.debug(f"Parsed response data: {response_data}")

        if response_data.get('violates_guidelines', False):
            return jsonify({
                'violates_guidelines': True,
                'explanation': response_data.get('explanation', 'No explanation provided.'),
                'suggestions': response_data.get('suggestions', [])
            }), 200
        
        # If no violations, create the post
        new_post = Post(content=content, user_id=current_user.id, timestamp=datetime.utcnow())
        
        if 'media' in request.files:
            file = request.files['media']
            if file and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                file.save(filepath)
                new_post.media_url = filename
        
        db.session.add(new_post)
        db.session.commit()
        
        return jsonify({'success': True, 'message': 'Your post has been created!'}), 200

    except Exception as e:
        logger.error(f"Error processing or saving post: {str(e)}", exc_info=True)
        return jsonify({'error': 'An error occurred while processing your post. Please try again.'}), 500

    


@app.route('/like/<int:post_id>', methods=['POST'])
@login_required
def like_post(post_id):
    post = Post.query.get_or_404(post_id)
    if current_user not in post.likes:
        post.likes.append(current_user)
    else:
        post.likes.remove(current_user)
    db.session.commit()
    return jsonify({'likes_count': len(post.likes), 'is_liked': current_user in post.likes})

@app.route('/comment/<int:post_id>', methods=['POST'])
@login_required
def add_comment(post_id):
    post = Post.query.get_or_404(post_id)
    content = request.json.get('content')
    if content:
        comment = Comment(content=content, author=current_user, post=post)
        db.session.add(comment)
        db.session.commit()
        return jsonify({
            'id': comment.id,
            'content': comment.content,
            'author': comment.author.username,
            'timestamp': comment.timestamp.strftime('%Y-%m-%d %H:%M:%S')
        }), 201
    return jsonify({'error': 'Comment content is required'}), 400

@app.route('/comment/<int:comment_id>', methods=['DELETE'])
@login_required
def delete_comment(comment_id):
    comment = Comment.query.get_or_404(comment_id)
    if comment.author != current_user:
        return jsonify({'error': 'Unauthorized'}), 403
    db.session.delete(comment)
    db.session.commit()
    return jsonify({'message': 'Comment deleted successfully'}), 200

@app.route('/delete-post/<int:post_id>', methods=['DELETE'])
def delete_post(post_id):
    post = Post.query.get(post_id)
    if post:
        db.session.delete(post)
        db.session.commit()
        return jsonify({"status": "success"}), 200
    return jsonify({"status": "error", "message": "Post not found"}), 404




@app.route('/conversations')

def conversations():
    return redirect(url_for('messages'))


@app.route('/messages/', defaults={'recipient_id': None})
@app.route('/messages/<int:recipient_id>')
@login_required
#@cache.cached(timeout=60, key_prefix='messages_%s')  # Cache for 1 minute
def messages(recipient_id):
    available_users = get_available_users()
    
    if recipient_id is None and available_users:
        recipient_id = available_users[0]['id']
    
    recipient = User.query.get(recipient_id) if recipient_id else None
    messages = get_messages(current_user.id, recipient_id) if recipient_id else []
    starters = get_conversation_starters(current_user.id, recipient_id) if recipient_id else []
    
    return render_template('messages.html', 
                           messages=messages, 
                           starters=starters, 
                           recipient=recipient, 
                           available_users=available_users,
                           current_user=current_user)


@app.route('/api/conversation_starters/<int:recipient_id>')
@login_required
def api_conversation_starters(recipient_id):
    if recipient_id is None:
        return jsonify({'error': 'No recipient specified'}), 400
    try:
        starters = get_conversation_starters(current_user.id, recipient_id)
        return jsonify({'starters': starters}), 200
    except Exception as e:
        app.logger.error(f"Error generating conversation starters: {str(e)}")
        return jsonify({'error': 'Failed to generate conversation starters'}), 500

def get_conversation_starters(user_id, other_user_id):
    user = User.query.get(user_id)
    other_user = User.query.get(other_user_id)
    
    if not user or not other_user:
        raise ValueError("User not found")
    
    general_starters = [
        "How's your day going so far?",
        "Any exciting plans for the weekend?",
        "What's your favorite way to relax after a long day?",
        "Have you read any good books or watched any interesting movies lately?",
        "If you could travel anywhere in the world right now, where would you go?",
        "What's the best piece of advice you've ever received?",
        "Do you have any hobbies or passions you'd like to share?",
        "What's your idea of a perfect day?",
        "If you could have dinner with any historical figure, who would it be and why?",
        "What's the most interesting place you've ever visited?"
    ]
    
    # You can add more specific starters based on user profiles if available
    specific_starters = []
    if user.bio and other_user.bio:
        specific_starters.append(f"I noticed in your bio that you mentioned {other_user.bio.split()[0]}. Could you tell me more about that?")
    
    # Combine and shuffle the starters
    all_starters = specific_starters + general_starters
    random.shuffle(all_starters)
    
    # Return 5 random starters
    return all_starters[:5]
@app.route('/send_message/<int:recipient_id>', methods=['POST'])
@login_required
def send_message_route(recipient_id):
    content = request.form['content']
    
    if not content.strip():
        return jsonify({"error": "Message content cannot be empty"}), 400

    new_message = Message(
        sender_id=current_user.id,
        recipient_id=recipient_id,
        content=content,
        timestamp=datetime.utcnow()
    )
    
    db.session.add(new_message)
    db.session.commit()

    return jsonify({
        'id': new_message.id,
        'sender_id': current_user.id,
        'recipient_id': recipient_id,
        'content': content,
        'timestamp': new_message.timestamp.isoformat(),
        'sender_username': current_user.username,
        'sender_profile_picture': current_user.profile_picture
    }), 200



def generate_ai_reply(content):
    prompt = f"""
    Given the following message, suggest a thoughtful and engaging reply:
    "{content}"
    Keep the reply concise and natural-sounding. Include appropriate emojis to make the message more engaging.
    Do not use asterisks or any other formatting. The reply should be ready to send as-is.
    """
    
    response = model.generate_content(prompt)
    
    # Check if the response contains text
    if hasattr(response, 'text') and response.text:
        # Remove any remaining asterisks from the response
        cleaned_response = response.text.replace('*', '')
        
        return cleaned_response
    else:
        # Handle the case where response does not contain text
        return "Sorry, I couldn't generate a reply."


@app.route('/generate_ai_reply/<int:recipient_id>', methods=['POST'])
@login_required
def api_generate_ai_reply(recipient_id):
    if recipient_id is None:
        return jsonify({'error': 'No recipient specified'}), 400
    try:
        # Fetch the latest message content from the chat with the recipient
        last_message = Message.query.filter(
            ((Message.sender_id == current_user.id) & (Message.recipient_id == recipient_id)) |
            ((Message.sender_id == recipient_id) & (Message.recipient_id == current_user.id))
        ).order_by(Message.timestamp.desc()).first()
        
        if last_message:
            content = last_message.content
            ai_reply = generate_ai_reply(content)
            
            # Send the AI reply to the chat
            new_message, error = send_message_helper(current_user.id, recipient_id, ai_reply)
            if error:
                return jsonify({'error': error}), 400
            
            message_data = {
                'id': new_message.id,
                'sender_id': current_user.id,
                'recipient_id': recipient_id,
                'content': ai_reply,
                'timestamp': new_message.timestamp.isoformat()
            }
            
            # Broadcast the AI message to both users
            socketio.emit('new_message', message_data, room=str(recipient_id))
            socketio.emit('new_message', message_data, room=str(current_user.id))
            
            return jsonify({'reply': ai_reply}), 200
        else:
            return jsonify({'error': 'No previous message found to base AI reply on'}), 400
    except Exception as e:
        app.logger.error(f"Error generating AI reply: {str(e)}")
        return jsonify({'error': 'Failed to generate AI reply'}), 500
    
def send_message_helper(sender_id, recipient_id, content, media_url=None):
    try:
        if not content.strip():
            return None, "Message content cannot be empty."

        new_message = Message(
            sender_id=sender_id,
            recipient_id=recipient_id,
            content=content,
            media_url=media_url,
            timestamp=datetime.utcnow()
        )

        db.session.add(new_message)
        db.session.commit()

        return new_message, None

    except Exception as e:
        db.session.rollback()
        return None, f"An error occurred: {str(e)}"
    
def get_messages(current_user_id, recipient_id, page=1, per_page=20):
    messages = db.session.query(Message, User).join(User, Message.sender_id == User.id).filter(
        or_(
            (Message.sender_id == current_user_id) & (Message.recipient_id == recipient_id),
            (Message.sender_id == recipient_id) & (Message.recipient_id == current_user_id)
        )
    ).order_by(Message.timestamp.asc()).paginate(page=page, per_page=per_page, error_out=False)

    return messages.items
@app.route('/delete_chat_history/<int:recipient_id>', methods=['POST'])
@login_required
def delete_chat_history(recipient_id):
    try:
        # Delete messages where the current user is either the sender or the recipient
        Message.query.filter(
            or_(
                (Message.sender_id == current_user.id) & (Message.recipient_id == recipient_id),
                (Message.sender_id == recipient_id) & (Message.recipient_id == current_user.id)
            )
        ).delete(synchronize_session=False)

        # Commit the changes to the database
        db.session.commit()

        return jsonify({"success": True, "message": "Chat history deleted successfully"}), 200
    except Exception as e:
        # If an error occurs, rollback the changes
        db.session.rollback()
        return jsonify({"success": False, "message": str(e)}), 500

def get_available_users():
    users = User.query.filter(User.id != current_user.id).all()
    return [{'id': user.id, 'username': user.username, 'profile_picture': user.profile_picture} for user in users]



@app.route('/notifications')
@login_required
def notifications():
    notifications = Notification.query.filter_by(user_id=current_user.id).order_by(Notification.timestamp.desc()).all()
    # Mark all notifications as read
    for notification in notifications:
        notification.read = True
    db.session.commit()
    return render_template('notifications.html', notifications=notifications)

from tasks import create_notification as create_notification_task

def create_notification(user_id, content):
    create_notification_task.delay(user_id, content)

    
def get_messages(current_user_id, recipient_id, page=1, per_page=20):
    messages = db.session.query(Message, User).join(User, Message.sender_id == User.id).filter(
        or_(
            (Message.sender_id == current_user_id) & (Message.recipient_id == recipient_id),
            (Message.sender_id == recipient_id) & (Message.recipient_id == current_user_id)
        )
    ).order_by(Message.timestamp.asc()).paginate(page=page, per_page=per_page, error_out=False)

    return messages.items


def get_available_users():
    users = User.query.filter(User.id != current_user.id).all()
    return [{'id': user.id, 'username': user.username, 'profile_picture': user.profile_picture} for user in users]





    
@socketio.on('typing')
def handle_typing(data):
    recipient_id = data['recipient_id']
    socketio.emit('typing', {'sender_id': current_user.id}, room=str(recipient_id))

@socketio.on('stop_typing')
def handle_stop_typing(data):
    recipient_id = data['recipient_id']
    socketio.emit('stop_typing', {'sender_id': current_user.id}, room=str(recipient_id))

@socketio.on('message_read')
def handle_message_read(data):
    message_id = data['message_id']
    message = Message.query.get(message_id)
    if message:
        message.read = True
        db.session.commit()
        socketio.emit('message_status_update', {'message_id': message_id, 'read': True}, room=str(message.sender_id))

@socketio.on('connect')
def handle_connect():
    if current_user.is_authenticated:
        join_room(str(current_user.id))

@socketio.on('disconnect')
def handle_disconnect():
    if current_user.is_authenticated:
        leave_room(str(current_user.id))

@app.before_request
def make_session_permanent():
    session.permanent = True
    app.permanent_session_lifetime = timedelta(days=30)
    
@cache.memoize(300)
def moderate_content(content):
    prompt = f"""
    Analyze the following content for appropriateness on a social media platform. Please take into account common community guidelines which may include but are not limited to: harassment, hate speech, violence, explicit content, misinformation, and spam.

    Content to analyze:
    "{content}"

    Please provide the following in your response:
    1. **Violates Guidelines**: Determine if the content violates any common social media community guidelines. Respond with `true` if it violates, otherwise `false`.
    2. **Explanation**: Provide a brief explanation for your determination. Mention which specific guideline(s) are potentially violated or why the content is considered appropriate.
    3. **Sentiment Analysis**: Analyze the sentiment of the content and classify it as `positive`, `neutral`, or `negative`. Provide reasoning for the sentiment classification.
    4. **Suggestions for Improvement**: If the content is borderline inappropriate or has potential issues, suggest specific ways to improve it to make it more suitable for a social media platform. 

    Format your response as a JSON object with the following keys:
    - `"violates_guidelines"`: (boolean) `true` or `false` indicating if the content violates guidelines.
    - `"explanation"`: (string) A brief explanation of why the content does or does not violate guidelines.
    - `"sentiment"`: (string) The sentiment analysis result, which can be `positive`, `neutral`, or `negative`.
    - `"suggestions"`: (array of strings) Suggestions for improving the content if needed.

    Example of a JSON response:
    {{
        "violates_guidelines": true,
        "explanation": "The content contains explicit language which violates our community guidelines on harassment.",
        "sentiment": "negative",
        "suggestions": ["Remove explicit language", "Rephrase the content to be more respectful."]
    }}
    """

    try:
        response = model.generate_content(prompt)
        response_text = getattr(response, 'text', '').strip()

        # Log the response text for debugging
        print(f"Response text: {response_text}")

        if not response_text:
            raise ValueError("Received an empty response from the model")

        # Try to parse the JSON response
        try:
            moderation_result = json.loads(response_text)
        except json.JSONDecodeError as e:
            print(f"Error decoding JSON: {e}")
            print(f"Response text: {response_text}")
            
            # Attempt to extract JSON from the response if it's not properly formatted
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if json_match:
                try:
                    moderation_result = json.loads(json_match.group())
                except json.JSONDecodeError:
                    raise ValueError("Unable to extract valid JSON from the model's response")
            else:
                raise ValueError("No JSON-like structure found in the model's response")

        # Validate the structure of the moderation result
        required_keys = ['violates_guidelines', 'explanation', 'sentiment', 'suggestions']
        if not all(key in moderation_result for key in required_keys):
            raise ValueError("Moderation result is missing required keys")

        # Check for vulgar language
        if moderation_result.get("violates_guidelines") and "explicit" in moderation_result.get("explanation", "").lower():
            moderation_result["suggestions"].append("Please avoid using vulgar language.")

        return moderation_result

    except Exception as e:
        print(f"Error in moderate_content: {str(e)}")
        # Return a default response in case of any error
        return {
            "violates_guidelines": False,
            "explanation": "Unable to analyze content due to an error.",
            "sentiment": "neutral",
            "suggestions": ["Please try again later."]
        }

if __name__ == '__main__':
    #with app.app_context():
      # db.create_all()
        socketio.run(app, debug=True)