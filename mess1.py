import os
from flask import jsonify
from flask_socketio import emit
from datetime import datetime
from models import db, Message, User
from sqlalchemy import or_
import google.generativeai as genai
from dotenv import load_dotenv
import nltk
from nltk.sentiment import SentimentIntensityAnalyzer

# Load environment variables
load_dotenv()

# Initialize Gemini AI
genai.configure(api_key=os.getenv('GEMINI_API_KEY'))
model = genai.GenerativeModel('gemini-pro')

# Initialize NLTK for sentiment analysis
nltk.download('vader_lexicon')
sia = SentimentIntensityAnalyzer()

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

        # Check message quality and community guidelines
        moderation_result = moderate_content(content)
        if not moderation_result['appropriate']:
            emit('message_rejected', {
                'reason': moderation_result['reason'],
                'sender_id': sender_id
            }, room=sender_id)
            return

        new_message = Message(
            sender_id=sender_id,
            recipient_id=recipient_id,
            content=content,
            media_url=media_url,
            timestamp=datetime.utcnow(),
            sentiment=analyze_sentiment(content)
        )
        db.session.add(new_message)
        db.session.commit()

        message_data = {
            'id': new_message.id,
            'sender_id': new_message.sender_id,
            'recipient_id': new_message.recipient_id,
            'content': new_message.content,
            'media_url': new_message.media_url,
            'timestamp': new_message.timestamp.isoformat(),
            'read': False,
            'delivered': True,
            'sentiment': new_message.sentiment
        }

        emit('new_message', message_data, room=recipient_id)
        emit('new_message', message_data, room=sender_id)

        # Generate AI response suggestion
        ai_suggestion = generate_ai_reply(content)
        emit('ai_reply_suggestion', {
            'suggestion': ai_suggestion,
            'recipient_id': recipient_id
        }, room=recipient_id)

    # ... (keep other existing Socket.IO event handlers)
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
    Please analyze the following message and determine if it follows community guidelines. 
    The message should not contain hate speech, explicit content, or violate user privacy.
    Respond with a JSON object containing 'appropriate' (boolean) and 'reason' (string) fields.
    
    Message: "{content}"
    """
    
    response = model.generate_content(prompt)
    result = eval(response.text)  # Convert the response to a Python dictionary
    return result

def analyze_sentiment(content):
    sentiment_scores = sia.polarity_scores(content)
    compound_score = sentiment_scores['compound']
    
    if compound_score >= 0.05:
        return 'positive'
    elif compound_score <= -0.05:
        return 'negative'
    else:
        return 'neutral'

def generate_ai_reply(content):
    prompt = f"""
    Given the following message, suggest a thoughtful and engaging reply:
    "{content}"
    Keep the reply concise and natural-sounding.
    """
    
    response = model.generate_content(prompt)
    return response.text

def get_messages(current_user_id, recipient_id, page=1, per_page=20):
    messages = Message.query.filter(
        or_(
            (Message.sender_id == current_user_id) & (Message.recipient_id == recipient_id),
            (Message.sender_id == recipient_id) & (Message.recipient_id == current_user_id)
        )
    ).order_by(Message.timestamp.desc()).paginate(page=page, per_page=per_page, error_out=False)

    return messages

def send_message(sender_id, recipient_id, content, media_url=None):
    moderation_result = moderate_content(content)
    if not moderation_result['appropriate']:
        return None, moderation_result['reason']

    sentiment = analyze_sentiment(content)
    new_message = Message(
        sender_id=sender_id,
        recipient_id=recipient_id,
        content=content,
        media_url=media_url,
        timestamp=datetime.utcnow(),
        sentiment=sentiment
    )
    db.session.add(new_message)
    db.session.commit()

    return new_message, None

def get_user_conversations(user_id):
    subquery = db.session.query(
        db.func.max(Message.id).label('max_id')
    ).filter(
        or_(Message.sender_id == user_id, Message.recipient_id == user_id)
    ).group_by(
        db.func.least(Message.sender_id, Message.recipient_id),
        db.func.greatest(Message.sender_id, Message.recipient_id)
    ).subquery()

    latest_messages = db.session.query(Message).join(
        subquery, Message.id == subquery.c.max_id
    ).order_by(Message.timestamp.desc()).all()

    conversations = []
    for message in latest_messages:
        other_user_id = message.recipient_id if message.sender_id == user_id else message.sender_id
        other_user = User.query.get(other_user_id)
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

def get_conversation_summary(user_id, other_user_id):
    messages = Message.query.filter(
        or_(
            (Message.sender_id == user_id) & (Message.recipient_id == other_user_id),
            (Message.sender_id == other_user_id) & (Message.recipient_id == user_id)
        )
    ).order_by(Message.timestamp.desc()).limit(10).all()

    conversation_text = "\n".join([f"{msg.sender_id}: {msg.content}" for msg in reversed(messages)])
    
    prompt = f"""
    Summarize the following conversation between two users:
    
    {conversation_text}
    
    Provide a brief summary of the main topics discussed and the overall tone of the conversation.
    """

    response = model.generate_content(prompt)
    return response.text

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