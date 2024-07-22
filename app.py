import os
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from flask_socketio import SocketIO, emit, join_room, leave_room
import google.generativeai as genai
from dotenv import load_dotenv
import datetime
from flask_migrate import Migrate

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.urandom(24).hex()
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///social_media.db'
app.config['UPLOAD_FOLDER'] = 'static/uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max-limit

db = SQLAlchemy(app)
migrate = Migrate(app, db)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
socketio = SocketIO(app)

# Configure the Gemini model
genai.configure(api_key=os.getenv('GEMINI_API_KEY'))
model = genai.GenerativeModel('gemini-pro')

# Database models
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(128))
    profile_picture = db.Column(db.String(120), default='default.jpg')
    bio = db.Column(db.Text)
    posts = db.relationship('Post', backref='author', lazy='dynamic')
    following = db.relationship('Follow',
                                foreign_keys='Follow.follower_id',
                                backref='follower',
                                lazy='dynamic')
    followers = db.relationship('Follow',
                                foreign_keys='Follow.followed_id',
                                backref='followed',
                                lazy='dynamic')

class Post(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    content = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    media_url = db.Column(db.String(120))
    likes = db.relationship('Like', backref='post', lazy='dynamic')
    comments = db.relationship('Comment', backref='post', lazy='dynamic')

class Like(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    post_id = db.Column(db.Integer, db.ForeignKey('post.id'), nullable=False)

class Comment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    content = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    post_id = db.Column(db.Integer, db.ForeignKey('post.id'), nullable=False)

class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    content = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    sender_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    recipient_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    read = db.Column(db.Boolean, default=False)

class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    content = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    read = db.Column(db.Boolean, default=False)

class Follow(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    follower_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    followed_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.datetime.utcnow)

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

@app.route('/create_post', methods=['POST'])
@login_required
def create_post():
    content = request.form['content']
    if 'media' in request.files:
        file = request.files['media']
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            media_url = url_for('static', filename=f'uploads/{filename}')
        else:
            media_url = None
    else:
        media_url = None
    
    new_post = Post(content=content, user_id=current_user.id, media_url=media_url)
    db.session.add(new_post)
    db.session.commit()
    
    # Moderate the content
    moderation_result = moderate_content(content)
    if moderation_result['violates_guidelines']:
        db.session.delete(new_post)
        db.session.commit()
        flash('Your post violates our community guidelines and has been removed.')
    else:
        flash('Post created successfully')
    
    return redirect(url_for('index'))

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


@app.route('/messages')
@login_required
def messages():
    users = User.query.filter(User.id != current_user.id).all()
    return render_template('messages.html', users=users)

@app.route('/chat/<int:user_id>')
@login_required
def chat(user_id):
    user = User.query.get_or_404(user_id)
    messages = Message.query.filter(
        ((Message.sender_id == current_user.id) & (Message.recipient_id == user_id)) |
        ((Message.sender_id == user_id) & (Message.recipient_id == current_user.id))
    ).order_by(Message.timestamp.asc()).all()
    return render_template('chat.html', user=user, messages=messages)

@app.route('/send_message/<int:recipient_id>', methods=['POST'])
@login_required
def send_message(recipient_id):
    content = request.form['content']
    new_message = Message(content=content, sender_id=current_user.id, recipient_id=recipient_id)
    db.session.add(new_message)
    db.session.commit()
    create_notification(recipient_id, f'You have a new message from {current_user.username}.')
    socketio.emit('new_message', {
        'sender_id': current_user.id,
        'recipient_id': recipient_id,
        'content': content
    }, room=str(recipient_id))
    return redirect(url_for('chat', user_id=recipient_id))

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
    Analyze the following content for appropriateness on a social media platform:

    "{content}"

    Please provide:
    1. A determination if the content violates common social media community guidelines.
    2. A brief explanation of your determination.
    3. A sentiment analysis (positive, neutral, or negative).
    4. Any suggestions for improvement if the content is borderline inappropriate.

    Format your response as a JSON object with keys: "violates_guidelines" (boolean), "explanation", "sentiment", and "suggestions".
    """

    response = model.generate_content(prompt)
    
    # Parse the model's response
    moderation_result = jsonify(response.text)  # Be cautious with eval in production!
    
    return moderation_result

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    socketio.run(app, debug=True)