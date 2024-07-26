import eventlet
eventlet.monkey_patch()

import os
from flask import Flask, jsonify, request
from flask_login import login_required, current_user
from flask_socketio import SocketIO, emit, join_room, leave_room
from datetime import datetime
import emojis
from models import db, Message, User
from sqlalchemy import case, or_
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)
genai.configure(api_key=os.getenv('GEMINI_API_KEY'))
model = genai.GenerativeModel('gemini-pro')

# Use eventlet for asynchronous operations
socketio = SocketIO(app, async_mode='eventlet')

@socketio.on('send_message')
async def handle_send_message(data):
    sender_id = data['sender_id']
    recipient_id = data['recipient_id']
    content = data['content']
    
    # Await the content moderation
    moderation_result = await moderate_content(content)
    if moderation_result['violates_guidelines']:
        emit('message_warning', {
            'reason': moderation_result['explanation'],
            'alternatives': moderation_result['suggestions'],
            'sender_id': sender_id
        }, room=sender_id)
        return
    
    # Await sending the message
    new_message, error = await send_message_helper(sender_id, recipient_id, content)
    if error:
        emit('message_error', {'error': error}, room=sender_id)
        return
    
    message_data = {
        'id': new_message.id,
        'sender_id': new_message.sender_id,
        'recipient_id': new_message.recipient_id,
        'content': new_message.content,
        'timestamp': new_message.timestamp.isoformat()
    }
    
    emit('new_message', message_data, room=recipient_id)
    emit('new_message', message_data, room=sender_id)

@socketio.on('join')
async def on_join(data):
    username = data['username']
    room = data['room']
    join_room(room)
    emit('status', {'msg': f'{username} has entered the room.'}, room=room)

@socketio.on('leave')
async def on_leave(data):
    username = data['username']
    room = data['room']
    leave_room(room)
    emit('status', {'msg': f'{username} has left the room.'}, room=room)

def init_mess(socketio):
    @socketio.on('connect')
    async def handle_connect():
        if current_user.is_authenticated:
            join_room(str(current_user.id))
            emit('user_connected', {'user_id': current_user.id, 'username': current_user.username})

    @socketio.on('disconnect')
    async def handle_disconnect():
        if current_user.is_authenticated:
            leave_room(str(current_user.id))
            emit('user_disconnected', {'user_id': current_user.id, 'username': current_user.username})

async def moderate_content(content):
    prompt = f"""
    Analyze the following message and determine if it follows community guidelines. 
    The message should not contain hate speech, explicit content, or violate user privacy.
    If inappropriate, suggest 3 alternative phrasings that convey a similar meaning in a more appropriate way.
    Response format:
    {{
        "violates_guidelines": boolean,
        "explanation": string,
        "suggestions": [string, string, string]
    }}
    
    Message: "{content}"
    """
    
    response = await model.generate_content(prompt)
    result = eval(response.text)
    return result

async def generate_ai_reply(content):
    prompt = f"""
    Given the following message, suggest a thoughtful and engaging reply:
    "{content}"
    Keep the reply concise and natural-sounding. Include appropriate emojis to make the message more engaging.
    Do not use asterisks or any other formatting. The reply should be ready to send as-is.
    """
    
    response = await model.generate_content(prompt)
    return emojis.emojize(response.text, language='alias')

async def send_message_helper(sender_id, recipient_id, content):
    new_message = Message(
        sender_id=sender_id,
        recipient_id=recipient_id,
        content=content,
        timestamp=datetime.utcnow()
    )
    db.session.add(new_message)
    db.session.commit()

    return new_message, None

async def get_messages(current_user_id, recipient_id, page=1, per_page=20):
    messages = Message.query.filter(
        or_(
            (Message.sender_id == current_user_id) & (Message.recipient_id == recipient_id),
            (Message.sender_id == recipient_id) & (Message.recipient_id == current_user_id)
        )
    ).order_by(Message.timestamp.desc()).paginate(page=page, per_page=per_page, error_out=False)

    return messages.items

async def get_user_conversations(user_id):
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

async def search_messages(current_user_id, recipient_id, query):
    messages = Message.query.filter(
        or_(
            (Message.sender_id == current_user_id) & (Message.recipient_id == recipient_id),
            (Message.sender_id == recipient_id) & (Message.recipient_id == current_user_id)
        ),
        Message.content.ilike(f'%{query}%')
    ).order_by(Message.timestamp.desc()).all()

    return messages

async def suggest_conversation_starters(user_id, other_user_id):
    user = User.query.get(user_id)
    other_user = User.query.get(other_user_id)

    prompt = f"""
    Suggest 3 conversation starters for two users based on their profiles:
    
    User 1: {user.bio}
    User 2: {other_user.bio}
    
    Provide engaging and relevant conversation starters that could help these users connect.
    """

    response = await model.generate_content(prompt)
    return response.text.split('\n')

async def get_available_users():
    users = User.query.filter(User.id != current_user.id).all()
    available_users = [{'id': user.id, 'username': user.username, 'profile_picture': user.profile_picture} for user in users]
    return available_users

if __name__ == '__main__':
    # Run the app with eventlet
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
