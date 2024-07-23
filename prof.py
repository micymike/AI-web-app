from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename
from models import Comment, Follow, User, Like, Message, Notification, Post, db
from datetime import datetime
import os
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

profile = Blueprint('prof', __name__)

# Configure Gemini AI
genai.configure(api_key=os.getenv('GEMINI_API_KEY'))
model = genai.GenerativeModel('gemini-pro')

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@profile.route('/profile/<username>')
@login_required
def user_profile(username):
    user = User.query.filter_by(username=username).first_or_404()
    posts = Post.query.filter_by(user_id=user.id).order_by(Post.timestamp.desc()).all()
    return render_template('profile.html', user=user, posts=posts)

@profile.route('/edit_profile', methods=['GET', 'POST'])
@login_required
def edit_profile():
    if request.method == 'POST':
        current_user.username = request.form['username']
        current_user.email = request.form['email']
        current_user.bio = request.form['bio']
        current_user.location = request.form['location']

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

@profile.route('/follow/<username>', methods=['POST'])
@login_required
def follow(username):
    user = User.query.filter_by(username=username).first()
    if user is None:
        flash('User not found.', 'error')
        return redirect(url_for('index'))
    if user == current_user:
        flash('You cannot follow yourself!', 'error')
        return redirect(url_for('prof.user_profile', username=username))
    current_user.follow(user)
    db.session.commit()
    flash(f'You are now following {username}!', 'success')
    return redirect(url_for('prof.user_profile', username=username))

@profile.route('/unfollow/<username>', methods=['POST'])
@login_required
def unfollow(username):
    user = User.query.filter_by(username=username).first()
    if user is None:
        flash('User not found.', 'error')
        return redirect(url_for('index'))
    if user == current_user:
        flash('You cannot unfollow yourself!', 'error')
        return redirect(url_for('prof.user_profile', username=username))
    current_user.unfollow(user)
    db.session.commit()
    flash(f'You have unfollowed {username}.', 'success')
    return redirect(url_for('prof.user_profile', username=username))

@profile.route('/followers/<username>')
@login_required
def followers(username):
    user = User.query.filter_by(username=username).first_or_404()
    followers = user.followers.all()
    return render_template('followers.html', user=user, followers=followers)

@profile.route('/following/<username>')
@login_required
def following(username):
    user = User.query.filter_by(username=username).first_or_404()
    following = user.following.all()
    return render_template('following.html', user=user, following=following)

@profile.route('/create_post', methods=['GET', 'POST'])
@login_required
def create_post():
    if request.method == 'POST':
        content = request.form['content']
        
        # Check content with AI for community guidelines
        response = model.generate_content(f"Check if the following content adheres to community guidelines and is not offensive or inappropriate: '{content}'")
        
        if "inappropriate" in response.text.lower() or "offensive" in response.text.lower():
            flash('Your post may violate community guidelines. Please review and try again.', 'warning')
            return redirect(url_for('prof.create_post'))

        new_post = Post(content=content, user_id=current_user.id, timestamp=datetime.utcnow())
        
        if 'media' in request.files:
            file = request.files['media']
            if file and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                file.save(os.path.join('static/uploads', filename))
                new_post.media_url = filename

        db.session.add(new_post)
        db.session.commit()
        flash('Your post has been created!', 'success')
        return redirect(url_for('prof.user_profile', username=current_user.username))

    return render_template('create_post.html')

@profile.route('/ai_assistant', methods=['GET', 'POST'])
@login_required
def ai_assistant():
    if request.method == 'POST':
        user_input = request.form['user_input']
        response = model.generate_content(f"User: {user_input}\nAI Assistant: ")
        return render_template('ai_assistant.html', user_input=user_input, ai_response=response.text)
    return render_template('ai_assistant.html')

@profile.route('/post/<int:post_id>/like', methods=['POST'])
@login_required
def like_post(post_id):
    post = Post.query.get_or_404(post_id)
    if current_user not in post.likes:
        post.likes.append(current_user)
        db.session.commit()
    return redirect(request.referrer)

@profile.route('/post/<int:post_id>/unlike', methods=['POST'])
@login_required
def unlike_post(post_id):
    post = Post.query.get_or_404(post_id)
    if current_user in post.likes:
        post.likes.remove(current_user)
        db.session.commit()
    return redirect(request.referrer)

@profile.route('/search')
@login_required
def search():
    query = request.args.get('query', '')
    users = User.query.filter(User.username.ilike(f'%{query}%')).all()
    posts = Post.query.filter(Post.content.ilike(f'%{query}%')).all()
    return render_template('search_results.html', users=users, posts=posts, query=query)