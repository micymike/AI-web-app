"""Add date_joined to User model

Revision ID: e9e4bf914113
Revises: 
Create Date: 2024-07-23 17:01:48.478287

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e9e4bf914113'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('comment', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_comment_post_id'), ['post_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_comment_user_id'), ['user_id'], unique=False)

    with op.batch_alter_table('follow', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_follow_followed_id'), ['followed_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_follow_follower_id'), ['follower_id'], unique=False)

    with op.batch_alter_table('like', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_like_post_id'), ['post_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_like_user_id'), ['user_id'], unique=False)

    with op.batch_alter_table('message', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_message_recipient_id'), ['recipient_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_message_sender_id'), ['sender_id'], unique=False)

    with op.batch_alter_table('notification', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_notification_user_id'), ['user_id'], unique=False)

    with op.batch_alter_table('post', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_post_user_id'), ['user_id'], unique=False)

    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('date_joined', sa.DateTime(), nullable=True))
        batch_op.alter_column('password_hash',
               existing_type=sa.VARCHAR(length=128),
               nullable=False)
        batch_op.create_index(batch_op.f('ix_user_email'), ['email'], unique=True)
        batch_op.create_index(batch_op.f('ix_user_username'), ['username'], unique=True)

    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_user_username'))
        batch_op.drop_index(batch_op.f('ix_user_email'))
        batch_op.alter_column('password_hash',
               existing_type=sa.VARCHAR(length=128),
               nullable=True)
        batch_op.drop_column('date_joined')

    with op.batch_alter_table('post', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_post_user_id'))

    with op.batch_alter_table('notification', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_notification_user_id'))

    with op.batch_alter_table('message', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_message_sender_id'))
        batch_op.drop_index(batch_op.f('ix_message_recipient_id'))

    with op.batch_alter_table('like', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_like_user_id'))
        batch_op.drop_index(batch_op.f('ix_like_post_id'))

    with op.batch_alter_table('follow', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_follow_follower_id'))
        batch_op.drop_index(batch_op.f('ix_follow_followed_id'))

    with op.batch_alter_table('comment', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_comment_user_id'))
        batch_op.drop_index(batch_op.f('ix_comment_post_id'))

    # ### end Alembic commands ###
