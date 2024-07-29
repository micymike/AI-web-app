


from models import Notification, db




def create_notification(user_id, content):
    new_notification = Notification(user_id=user_id, content=content)
    db.session.add(new_notification)
    db.session.commit()
    
    
    
