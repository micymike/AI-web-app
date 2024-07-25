import json
import re
import os
import logging
from flask import Flask, abort, render_template, request, jsonify, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask import Blueprint
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from flask_socketio import SocketIO, emit, join_room, leave_room
import google.generativeai as genai
from dotenv import load_dotenv
from prof import profile as profile_blueprint
import datetime
from mess import get_conversation_summary, init_mess, get_messages, send_message_helper, get_user_conversations, search_messages, suggest_conversation_starters
from flask_migrate import Migrate
from sqlalchemy.exc import SQLAlchemyError
from models import Comment, Follow, Like, Message, Notification, User, Post, db

# Configure logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Initialize Flask app
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
app.register_blueprint(profile_blueprint)

# Initialize LoginManager
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# Initialize SocketIO
socketio = SocketIO(app)

# Configure the Gemini model
genai.configure(api_key=os.getenv('GEMINI_API_KEY'))
model = genai.GenerativeModel('gemini-pro')

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
    return redirect(url_for('profile', username=username'))

@app.route('/post', methods=['GET', 'POST'])
@login_required
def post():
    if request.method == 'POST':
        user_input = request.form.get('content', '').strip()

        if not user_input:
            flash('Post content cannot be empty!', 'warning')
            return redirect(url_for('index'))

        # Use Gemini model to check for community guideline violations
        prompt = f"""
        Analyze the following text for any violations of community guidelines. 
        If violations are found, provide an explanation and suggest alternative wordings.
        Text to analyze: "{user_input}"
        
        Respond in the following JSON format:
        {{
            "violates_guidelines": boolean,
            "explanation": "string",
            "suggestions": ["string"]
        }}
        """

        try:
            response = model.generate(prompt=prompt)
            response_text = response.get('text', '').strip()
            logger.debug(f"Raw Gemini response: {response_text}")
            
            # Extract JSON from the response
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if json_match:
                response_data = json.loads(json_match.group(0))
            else:
                raise ValueError("No valid JSON found in the response")
            
            logger.debug(f"Parsed response data: {response_data}")

            if response_data.get('violates_guidelines', False):
                explanation = response_data.get('explanation', 'No explanation provided.')
                suggestions = response_data.get('suggestions', [])

                flash(f"Warning: {explanation}", 'warning')
                if suggestions:
                    flash("Suggested Alternatives:", 'info')
                    for suggestion in suggestions:
                        flash(f"- {suggestion}", 'info')
                return render_template('index.html', original_content=user_input)

            # If we've reached this point, the content is okay to post
            new_post = Post(content=user_input, user_id=current_user.id, timestamp=datetime.utcnow())
            
            if 'media' in request.files:
                file = request.files['media']
                if file and allowed_file(file.filename):
                    filename = secure_filename(file.filename)
                    file.save(os.path.join('static/uploads', filename))
                    new_post.media_url = filename
            
            db.session.add(new_post)
            db.session.commit()
            flash('Your post has been created!', 'success')

        except Exception as e:
            logger.error(f"Error processing or saving post: {str(e)}", exc_info=True)
            flash(f'An error occurred while processing your post. Please try again.', 'danger')
        
        return redirect(url_for('index'))

    return render_template('index.html')

@app.route('/messages/', defaults={'recipient_id': None})
@app.route('/messages/<int:recipient_id>', methods=['GET', 'POST'])
@login_required
def messages(recipient_id):
    if recipient_id is None:
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

@app.route('/notifications')
@login_required
def notifications():
    notifications = Notification.query.filter_by(user_id=current_user.id).order_by(Notification.timestamp.desc()).all()
    return render_template('notifications.html', notifications=notifications)

@app.route('/notifications/mark_as_read/<int:notification_id>', methods=['POST'])
@login_required
def mark_as_read(notification_id):
    notification = Notification.query.get(notification_id)
    if notification and notification.user_id == current_user.id:
        notification.read = True
        db.session.commit()
        return jsonify({'status': 'success'})
    return jsonify({'status': 'error', 'message': 'Notification not found or access denied'})

@app.route('/search', methods=['GET'])
@login_required
def search():
    query = request.args.get('query', '')
    if query:
        search_results = search_messages(query, current_user.id)
        return render_template('search_results.html', results=search_results)
    return redirect(url_for('index'))

# SocketIO events
@socketio.on('join')
def on_join(data):
    room = data['room']
    join_room(room)
    emit('status', {'msg': f"{current_user.username} has entered the room."}, room=room)

@socketio.on('leave')
def on_leave(data):
    room = data['room']
    leave_room(room)
    emit('status', {'msg': f"{current_user.username} has left the room."}, room=room)

@socketio.on('message')
def handle_message(data):
    room = data['room']
    emit('message', {'msg': data['msg'], 'username': current_user.username}, room=room)

if __name__ == '__main__':
    socketio.run(app, debug=True)
