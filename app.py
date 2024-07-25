import json
import logging
import re
import os
from flask import Flask, abort, render_template, request, jsonify, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask import Blueprint
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
import requests
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from flask_socketio import SocketIO, emit, join_room, leave_room
import google.generativeai as genai
from dotenv import load_dotenv
from prof import profile as profile_blueprint
import datetime
from mess import get_conversation_summary, init_mess, get_messages, send_message_helper, get_user_conversations, search_messages, suggest_conversation_starters
from flask_migrate import Migrate
from flask_login import current_user
from datetime import datetime, timedelta, timezone
from models import Comment, Follow, Like, Message, Notification, User, Post, db
from sqlalchemy.exc import SQLAlchemyError


from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from models import db  # Import the db instance from models
import os

load_dotenv()
app = Flask(__name__)

# Configure your database URI
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///social_media.db'  # Change this as needed
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.urandom(24).hex()
app.config['UPLOAD_FOLDER'] = 'static/uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max-limit

# Initialize the SQLAlchemy instance with the Flask app
db.init_app(app)

# Initialize Flask-Migrate
migrate = Migrate(app, db)

# Import and register blueprints here
from prof import profile as profile_blueprint  # Import your blueprint
app.register_blueprint(profile_blueprint)

login_manager = LoginManager(app)
login_manager.login_view = 'login'
socketio = SocketIO(app)

# Configure the Gemini model
genai.configure(api_key=os.getenv('GEMINI_API_KEY'))
model = genai.GenerativeModel('gemini-pro')

# Database models



@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

def is_valid_input(text):
    return text and len(text.strip()) > 0

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in {'png', 'jpg', 'jpeg', 'gif', 'mp4'}

@app.route('/')
@login_required
def index():
    followed_users = [follow.followed_id for follow in current_user.following]
    posts = Post.query.filter(Post.user_id.in_(followed_users + [current_user.id])).order_by(Post.timestamp.desc()).all()
    return render_template('index.html', posts=posts)

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
@login_required
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

@app.route('/profile/<username>')
@login_required
def profile(username):
    user = User.query.filter_by(username=username).first_or_404()
    posts = Post.query.filter_by(user_id=user.id).order_by(Post.timestamp.desc()).all()
    return render_template('profile.html', user=user, posts=posts)

@app.route('/edit_profile', methods=['GET', 'POST'])
@login_required
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

@app.route('/follow/<username>')
@login_required
def follow(username):
    user = User.query.filter_by(username=username).first()
    if user is None:
        flash('User not found.')
        return redirect(url_for('index'))
    if user == current_user:
        flash('You cannot follow yourself!')
        return redirect(url_for('profile', username=username))
    current_user.following.append(Follow(followed=user))
    db.session.commit()
    flash(f'You are now following {username}!')
    return redirect(url_for('profile', username=username))

@app.route('/unfollow/<username>')
@login_required
def unfollow(username):
    user = User.query.filter_by(username=username).first()
    if user is None:
        flash('User not found.')
        return redirect(url_for('index'))
    if user == current_user:
        flash('You cannot unfollow yourself!')
        return redirect(url_for('profile', username=username))
    follow = current_user.following.filter_by(followed_id=user.id).first()
    if follow:
        db.session.delete(follow)
        db.session.commit()
        flash(f'You have unfollowed {username}.')
    return redirect(url_for('profile', username=username))

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Configure the Gemini model
genai.configure(api_key=os.getenv('GEMINI_API_KEY'))
model = genai.GenerativeModel('gemini-pro')

@app.route('/post', methods=['GET', 'POST'])
@login_required
def post():
    if request.method == 'POST':
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

            # If we've reached this point, the content is okay to post
            new_post = Post(content=user_input, user_id=current_user.id, timestamp=datetime.utcnow())
            
            if 'media' in request.files:
                file = request.files['media']
                if file and allowed_file(file.filename):
                    filename = secure_filename(file.filename)
                    file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                    new_post.media_url = filename
            
            db.session.add(new_post)
            db.session.commit()
            
            return jsonify({'success': True, 'message': 'Your post has been created!'}), 200

        except Exception as e:
            logger.error(f"Error processing or saving post: {str(e)}", exc_info=True)
            return jsonify({'error': 'An error occurred while processing your post. Please try again.'}), 500

    return render_template('create_post.html')

@app.route('/submit_post', methods=['POST'])
@login_required
def submit_post():
    content = request.form.get('content', '').strip()
    if not content:
        return jsonify({'error': 'Post content cannot be empty!'}), 400

    new_post = Post(content=content, user_id=current_user.id, timestamp=datetime.utcnow())
    
    if 'media' in request.files:
        file = request.files['media']
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            new_post.media_url = filename
    
    db.session.add(new_post)
    db.session.commit()
    
    return jsonify({'success': True, 'message': 'Your post has been created!'}), 200
    
@login_required
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
    like = Like.query.filter_by(user_id=current_user.id, post_id=post_id).first()
    if like:
        db.session.delete(like)
        db.session.commit()
        return jsonify({'status': 'unliked'})
    else:
        new_like = Like(user_id=current_user.id, post_id=post_id)
        db.session.add(new_like)
        db.session.commit()
        create_notification(post.author.id, f'{current_user.username} liked your post.')
        return jsonify({'status': 'liked'})

@app.route('/comment/<int:post_id>', methods=['POST'])
@login_required
def add_comment(post_id):
    content = request.form['content']
    new_comment = Comment(content=content, user_id=current_user.id, post_id=post_id)
    db.session.add(new_comment)
    db.session.commit()
    post = Post.query.get_or_404(post_id)
    create_notification(post.author.id, f'{current_user.username} commented on your post.')
    return redirect(url_for('index'))

@app.route('/delete_post/<int:post_id>', methods=['POST'])
@login_required
def delete_post(post_id):
    post = Post.query.get_or_404(post_id)
    if post.author != current_user:
        return jsonify({'status': 'error', 'message': 'Unauthorized'}), 403
    db.session.delete(post)
    db.session.commit()
    return jsonify({'status': 'success', 'message': 'Post deleted successfully'})

@app.route('/delete_comment/<int:comment_id>', methods=['POST'])
@login_required
def delete_comment(comment_id):
    comment = Comment.query.get_or_404(comment_id)
    if comment.author != current_user:
        return jsonify({'status': 'error', 'message': 'Unauthorized'}), 403
    db.session.delete(comment)
    db.session.commit()
    return jsonify({'status': 'success', 'message': 'Comment deleted successfully'})
#@app.route('/messages', methods=['GET', 'POST'])
from flask import render_template, request, jsonify, redirect, url_for
from flask_login import login_required, current_user

@app.route('/messages/', defaults={'recipient_id': None})
@app.route('/messages/<int:recipient_id>', methods=['GET', 'POST'])
@login_required
def messages(recipient_id):
    if recipient_id is None:
        # If no recipient_id is provided, redirect to a default page or show all conversations
        return redirect(url_for('conversations'))  # Assuming you have a 'conversations' route

    if request.method == 'POST':
        content = request.form['content']
        media_url = request.form.get('media_url')
        message, error = send_message_helper(current_user.id, recipient_id, content, media_url)
        if error:
            return jsonify({'status': 'error', 'message': error})
        return jsonify({'status': 'success', 'message': message.to_dict()})
    else:
        messages = get_messages(current_user.id, recipient_id)
        summary = get_conversation_summary(current_user.id, recipient_id)
        starters = suggest_conversation_starters(current_user.id, recipient_id)
        recipient = User.query.get(recipient_id)  # Fetch the recipient user
        return render_template('messages.html', messages=messages, summary=summary, starters=starters, recipient=recipient)
def get_all_users(current_user_id):
    return User.query.filter(User.id != current_user_id).all()
@app.route('/conversations')
@login_required
def conversations():
    user_conversations = get_user_conversations(current_user.id)
    all_users = get_all_users(current_user.id)
    return render_template('conversations.html', conversations=user_conversations, all_users=all_users)
@app.route('/api/conversation_summary/<int:other_user_id>')
@login_required
def api_conversation_summary(other_user_id):
    summary = get_conversation_summary(current_user.id, other_user_id)
    return jsonify({'summary': summary})

@app.route('/api/conversation_starters/<int:other_user_id>')
@login_required
def api_conversation_starters(other_user_id):
    starters = suggest_conversation_starters(current_user.id, other_user_id)
    return jsonify({'starters': starters})

@app.route('/send_message/<int:recipient_id>', methods=['POST'])
@login_required
def send_message_route(recipient_id):
    content = request.form['content']
    media = request.files.get('media')
    media_url = None
    
    if media and allowed_file(media.filename):
        filename = secure_filename(media.filename)
        media_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        media.save(media_path)
        media_url = url_for('static', filename=f'uploads/{filename}')
    
    new_message = Message(content=content, sender_id=current_user.id, recipient_id=recipient_id, media_url=media_url)
    db.session.add(new_message)
    db.session.commit()
    
    socketio.emit('new_message', {
        'id': new_message.id,
        'sender_id': current_user.id,
        'recipient_id': recipient_id,
        'content': content,
        'media_url': media_url,
        'timestamp': new_message.timestamp.isoformat()
    }, room=str(recipient_id))
    
    return jsonify({'status': 'success', 'message': 'Message sent successfully'})

def send_message_helper(sender_id, recipient_id, content, media_url=None):
    try:
        # Check if content is empty
        if not content.strip():
            return None, "Message content cannot be empty."

        # Create a new message
        new_message = Message(
            sender_id=sender_id,
            recipient_id=recipient_id,
            content=content,
            media_url=media_url
        )

        # Add the new message to the database
        db.session.add(new_message)
        db.session.commit()

        # If everything went well, return the message and no error
        return new_message, None

    except SQLAlchemyError as e:
        # If there's a database error, rollback the session
        db.session.rollback()
        return None, f"Database error: {str(e)}"

    except Exception as e:
        # For any other unexpected errors
        return None, f"An unexpected error occurred: {str(e)}"


@app.route('/notifications')
@login_required
def notifications():
    notifications = Notification.query.filter_by(user_id=current_user.id).order_by(Notification.timestamp.desc()).all()
    return render_template('notifications.html', notifications=notifications)

def create_notification(user_id, content):
    new_notification = Notification(user_id=user_id, content=content)
    db.session.add(new_notification)
    db.session.commit()
    socketio.emit('new_notification', {'user_id': user_id, 'content': content}, room=str(user_id))
    
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
