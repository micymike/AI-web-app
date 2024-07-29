import json
import logging
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
from sqlalchemy import or_, case

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

login_manager = LoginManager(app)
login_manager.login_view = 'login'
socketio = SocketIO(app, message_queue='memory://')

genai.configure(api_key=os.getenv('GEMINI_API_KEY'))
model = genai.GenerativeModel('gemini-pro')

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


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
@cache.cached(timeout=60) 
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


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form['username']).first()
        if user and check_password_hash(user.password_hash, request.form['password']):
            login_user(user)
            return redirect(url_for('index'))
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


@app.route('/profile/<username>')
@cache.cached(timeout=300) 
def user_profile(username):
    user = User.query.filter_by(username=username).first_or_404()
    posts = Post.query.filter_by(user_id=user.id).all()
    followers_count = user.followers.count()  # Assuming you have a way to get the count of followers
    return render_template('profile.html', user=user, posts=posts, followers_count=followers_count)


@app.route('/edit_profile', methods=['GET', 'POST'])

def edit_profile():
    if request.method == 'POST':
        current_user.bio = request.form['bio']
        if 'profile_picture' in request.files:
            file = request.files['profile_picture']
            if file and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                current_user.profile_picture = filename
        db.session.commit()
        flash('Profile updated successfully')
        return redirect(url_for('profile', username=current_user.username))
    return render_template('edit_profile.html')

@app.route('/api/follow/<username>', methods=['POST'])
@login_required
def follow(username):
    user = User.query.filter_by(username=username).first()
    if user is None:
        return jsonify({'error': 'User not found.'}), 404
    if user == current_user:
        return jsonify({'error': 'You cannot follow yourself!'}), 400
    
    if not current_user.is_following(user):
        current_user.following.append(Follow(followed=user))
        db.session.commit()
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
    
    follow = current_user.following.filter_by(followed_id=user.id).first()
    if follow:
        db.session.delete(follow)
        db.session.commit()
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


def is_following(self, user):
    return self.following.filter_by(followed_id=user.id).first() is not None


User.is_following = is_following

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

@app.route('/post', methods=['POST'])
@login_required
def post():
    user_input = request.form.get('content', '').strip()
    if not user_input:
        return jsonify({'error': 'Post content cannot be empty!'}), 400

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
                return jsonify({'error': 'An error occurred while processing your post. Please try again.'}), 500
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
        
        # Redirect to the index page after successful post creation
        return redirect(url_for('index'))

    except Exception as e:
        logger.error(f"Error processing or saving post: {str(e)}", exc_info=True)
        return jsonify({'error': 'An error occurred while processing your post. Please try again.'}), 500
@app.route('/submit_post', methods=['POST'])
@login_required
def submit_post():
    content = request.form.get('content', '').strip()
    if not content:
        return jsonify({'error': 'Post content cannot be empty!'}), 400

    # Use Gemini model to check for community guideline violations
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
        
        # Extract JSON from the response
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

    

def chat():
    if request.method == 'POST':
        user_input = request.form.get('user_input', '')
        response = model.generate_content(f"User: {user_input}\nAI Assistant: ")
        ai_response = response.text
        
        # Check if the response suggests the content might be inappropriate
        if "inappropriate" in ai_response.lower() or "offensive" in ai_response.lower():
            flash('Your input might be inappropriate. Please revise and try again.', 'warning')
            return redirect(url_for('index'))

        return jsonify({'response': ai_response})

    return render_template('index.html')
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

@app.route('/delete_post/<int:post_id>', methods=['DELETE'])

def delete_post(post_id):
    post = Post.query.get_or_404(post_id)
    if post.author != current_user:
        return jsonify({'status': 'error', 'message': 'Unauthorized'}), 403
    
    # Manually delete all comments associated with the post
    for comment in post.comments:
        db.session.delete(comment)
    
    db.session.delete(post)
    db.session.commit()
    
    return jsonify({'status': 'success', 'message': 'Post deleted successfully'}), 200





@app.route('/conversations')

def conversations():
    return redirect(url_for('messages'))


@app.route('/messages/', defaults={'recipient_id': None})
@app.route('/messages/<int:recipient_id>')
@login_required
@cache.cached(timeout=60, key_prefix='messages_%s')  # Cache for 1 minute
def messages(recipient_id):
    available_users = get_available_users()
    
    if recipient_id is None and available_users:
        recipient_id = available_users[0]['id']
    
    recipient = User.query.get(recipient_id) if recipient_id else None
    messages = get_messages(current_user.id, recipient_id) if recipient_id else []
    starters = suggest_conversation_starters(current_user.id, recipient_id) if recipient_id else []
    
    return render_template('messages.html', 
                           messages=messages, 
                           starters=starters, 
                           recipient=recipient, 
                           available_users=available_users,
                           current_user=current_user)


@app.route('/api/conversation_starters/<int:other_user_id>')
@cache.memoize(timeout=300) 
def api_conversation_starters(other_user_id):
    starters = suggest_conversation_starters(current_user.id, other_user_id)
    return jsonify({'starters': starters})

@app.route('/send_message/<int:recipient_id>', methods=['POST'])
@login_required
@cache.memoize(300)
def send_message_route(recipient_id):
    content = request.form['content']
    media = request.files.get('media')
    media_url = None
    ai_response_flag = request.form.get('ai_response', 'false').lower() == 'true'
    
    
    
    moderation_result = moderate_content(content)
    if moderation_result['violates_guidelines']:
        flash('Content violates guidelines: ' + moderation_result['explanation'])
        return redirect(url_for('messages', recipient_id=recipient_id))
    
    if media and allowed_file(media.filename):
        filename = secure_filename(media.filename)
        media_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        media.save(media_path)
        media_url = url_for('static', filename=f'uploads/{filename}')
    
    new_message, error = send_message_helper(current_user.id, recipient_id, content, media_url)
    
    if error:
        flash('Error sending message: ' + error)
        return redirect(url_for('messages', recipient_id=recipient_id))
    
    message_data = {
        'id': new_message.id,
        'sender_id': current_user.id,
        'recipient_id': recipient_id,
        'content': content,
        'media_url': media_url,
        'timestamp': new_message.timestamp.isoformat()
    }
    
    socketio.emit('new_message', message_data, room=str(recipient_id))
    socketio.emit('new_message', message_data, room=str(current_user.id))
    
    # Generate AI reply if the flag is set
    if ai_response_flag:
        ai_reply = generate_ai_reply(content)
        if ai_reply:
            ai_message, _ = send_message_helper(recipient_id, current_user.id, ai_reply)
            ai_message_data = {
                'id': ai_message.id,
                'sender_id': recipient_id,
                'recipient_id': current_user.id,
                'content': ai_reply,
                'timestamp': ai_message.timestamp.isoformat()
            }
            socketio.emit('new_message', ai_message_data, room=str(current_user.id))


    
    return redirect(url_for('messages', recipient_id=recipient_id))



@cache.memoize(timeout=600) 
def generate_ai_reply(content):
    prompt = f"""
    Given the following message, suggest a thoughtful and engaging reply:
    "{content}"
    Keep the reply concise and natural-sounding. Include appropriate emojis to make the message more engaging.
    Do not use asterisks or any other formatting. The reply should be ready to send as-is.
    """
    
    response = model.generate_content(prompt)
    
    # Use the emojis library to add emojis to the response text
    return emojis.encode(response.text, language='alias')


@app.route('/generate_ai_reply/<int:recipient_id>', methods=['GET'])
@login_required
@cache.memoize(timeout=600) 
def api_generate_ai_reply(recipient_id):
    # Fetch the latest message content from the chat with the recipient
    last_message = Message.query.filter_by(recipient_id=recipient_id, sender_id=current_user.id).order_by(Message.timestamp.desc()).first()
    
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

def suggest_conversation_starters(user_id, other_user_id):
    user = User.query.get(user_id)
    other_user = User.query.get(other_user_id)
    prompt = f"""
        Suggest 3 conversation starters for two users based on their profiles:
        
        User 1: {user.bio}
        User 2: {other_user.bio}
        
        Provide engaging and relevant conversation starters that could help these users connect. Include relevant emojis and exclude asterisks in your responses.
        """

    response = model.generate_content(prompt)
    return response.text.split('\n')

@app.route('/notifications')
@login_required
@cache.cached(timeout=60, key_prefix='notifications_%s')
def notifications():
    notifications = Notification.query.filter_by(user_id=current_user.id).order_by(Notification.timestamp.desc()).all()
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

@cache.memoize(300)
def suggest_conversation_starters(user_id, other_user_id):
    user = User.query.get(user_id)
    other_user = User.query.get(other_user_id)
    prompt = f"""
        Suggest 3 conversation starters for two users based on their profiles:
        
        User 1: {user.bio}
        User 2: {other_user.bio}
        
        Provide engaging and relevant conversation starters that could help these users connect.
        """

    response = model.generate_content(prompt)
    return response.text.split('\n')


    
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
    with app.app_context():
        db.create_all()
    socketio.run(app, debug=True)