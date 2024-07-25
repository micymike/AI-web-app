import os
from flask import Flask, jsonify
from flask_socketio import SocketIO, emit
from datetime import datetime
import emojis
import socketio

from models import db, Message, User
from sqlalchemy import case, or_
import google.generativeai as genai
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
app = Flask(__name__)
# Initialize Gemini AI
genai.configure(api_key=os.getenv('GEMINI_API_KEY'))
model = genai.GenerativeModel('gemini-pro')
socketio = SocketIO(app)
def init_mess(socketio):
    @socketio.on('connect')
    def handle_connect():
        print('Client connected')

    @socketio.on('disconnect')
    def handle_disconnect():
        print('Client disconnected')

@socketio.on('send_message')
def handle_send_message(data):
    sender_id = data['sender_id']
    recipient_id = data['recipient_id']
    content = data['content']
    media_url = data.get('media_url')
    
    ai_suggestion = generate_ai_reply(content)
    emit('ai_reply_suggestion', {
        'suggestion': ai_suggestion,
        'recipient_id': recipient_id
    }, room=recipient_id)

    moderation_result = moderate_content(content)
    if not moderation_result['appropriate']:
        emit('message_warning', {
            'reason': moderation_result['reason'],
            'alternatives': moderation_result['alternatives'],
            'sender_id': sender_id
        }, room=sender_id)
        return
    @socketio.on('message_read')
    def handle_message_read(data):
        message_id = data['message_id']
        message = Message.query.get(message_id)
        if message:
            message.read = True
            db.session.commit()
            emit('message_status_update', {'message_id': message_id, 'read': True}, room=message.sender_id)

    @socketio.on('typing')
    def handle_typing(data):
        sender_id = data['sender_id']
        recipient_id = data['recipient_id']
        emit('typing', {'sender_id': sender_id}, room=recipient_id)

    @socketio.on('stop_typing')
    def handle_stop_typing(data):
        sender_id = data['sender_id']
        recipient_id = data['recipient_id']
        emit('stop_typing', {'sender_id': sender_id}, room=recipient_id)

def moderate_content(content):
    prompt = f"""
    Analyze the following message and determine if it follows community guidelines. 
    The message should not contain hate speech, explicit content, or violate user privacy.
    If inappropriate, suggest 3 alternative phrasings that convey a similar meaning in a more appropriate way.
    Response format:
    {{
        "appropriate": boolean,
        "reason": string,
        "alternatives": [string, string, string]
    }}
    
    Message: "{content}"
    """

    response = model.generate_content(prompt)
    result = eval(response.text)
    return result

def generate_ai_reply(content):
    prompt = f"""
    Given the following message, suggest a thoughtful and engaging reply:
    "{content}"
    Keep the reply concise and natural-sounding. Include appropriate emojis to make the message more engaging.
    Do not use asterisks or any other formatting. The reply should be ready to send as-is.
    """
    
    response = model.generate_content(prompt)
    return emojis.emojize(response.text, language='alias')

def get_messages(current_user_id, recipient_id, page=1, per_page=20):
    messages = Message.query.filter(
        or_(
            (Message.sender_id == current_user_id) & (Message.recipient_id == recipient_id),
            (Message.sender_id == recipient_id) & (Message.recipient_id == current_user_id)
        )
    ).order_by(Message.timestamp.desc()).paginate(page=page, per_page=per_page, error_out=False)

    return messages

def send_message_helper(sender_id, recipient_id, content, media_url=None):
    moderation_result = moderate_content(content)
    if not moderation_result['appropriate']:
        return None, moderation_result['reason']

    new_message = Message(
        sender_id=sender_id,
        recipient_id=recipient_id,
        content=content,
        media_url=media_url,
        timestamp=datetime.utcnow(),
        sentiment='neutral'  # Sentiment analysis removed
    )
    db.session.add(new_message)
    db.session.commit()

    return new_message, None

def get_user_conversations(user_id):
    subquery = db.session.query(
        db.func.max(Message.id).label('max_id'),
        case(
            (Message.sender_id == user_id, Message.recipient_id),
            else_=Message.sender_id
        ).label('other_user_id')
    ).filter(
        (Message.sender_id == user_id) | (Message.recipient_id == user_id)
    ).group_by(
        case(
            (Message.sender_id == user_id, Message.recipient_id),
            else_=Message.sender_id
        )
    ).subquery()

    latest_messages = db.session.query(Message, User).join(
        subquery, Message.id == subquery.c.max_id
    ).join(
        User, User.id == subquery.c.other_user_id
    ).order_by(Message.timestamp.desc()).all()

    conversations = []
    for message, other_user in latest_messages:
        conversations.append({
            'user': other_user,
            'last_message': message
        })

    return conversations

def search_messages(current_user_id, recipient_id, query):
    messages = Message.query.filter(
        or_(
            (Message.sender_id == current_user_id) & (Message.recipient_id == recipient_id),
            (Message.sender_id == recipient_id) & (Message.recipient_id == current_user_id)
        ),
        Message.content.ilike(f'%{query}%')
    ).order_by(Message.timestamp.desc()).all()

    return messages

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

# Function to get a list of available users with profile pics
def get_available_users():
    users = User.query.all()
    available_users = [{'id': user.id, 'name': user.name, 'profile_pic': user.profile_pic_url} for user in users]
    return available_users
