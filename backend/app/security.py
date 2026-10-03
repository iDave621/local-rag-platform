import base64, hashlib, hmac, os, secrets
from datetime import datetime,timedelta,timezone
from fastapi import HTTPException,Request
from sqlalchemy import select,delete
from .db import db_session,User,AuthSession,AuditEvent
from .config import get_settings

def _profile(user):
    settings = get_settings()
    return {'id': user.id, 'username': user.username, 'role': user.role, 'active': user.active,
            'max_message_chars': settings.technician_max_message_chars if user.role == 'technician' else 12000}

def hash_password(password):
    salt=os.urandom(16); d=hashlib.scrypt(password.encode(),salt=salt,n=2**14,r=8,p=1,dklen=32); return f'scrypt${base64.b64encode(salt).decode()}${base64.b64encode(d).decode()}'
def verify_password(password,encoded):
    try:
        _,s,d=encoded.split('$',2); salt=base64.b64decode(s); expected=base64.b64decode(d); actual=hashlib.scrypt(password.encode(),salt=salt,n=2**14,r=8,p=1,dklen=len(expected)); return hmac.compare_digest(actual,expected)
    except Exception:return False
def _th(t):return hashlib.sha256(t.encode()).hexdigest()
def authenticate(username,password):
    with db_session() as db:
        u=db.scalar(select(User).where(User.username==username,User.active.is_(True)))
        if not u or not verify_password(password,u.password_hash):raise HTTPException(401,'שם משתמש או סיסמה שגויים')
        token=secrets.token_urlsafe(32);db.add(AuthSession(token_hash=_th(token),user_id=u.id,expires_at=datetime.now(timezone.utc)+timedelta(hours=12)));db.add(AuditEvent(user_id=u.id,action='login',detail=''));return _profile(u),token
def current_user(request:Request):
    t=request.cookies.get('rag_auth')
    if not t:raise HTTPException(401,'Authentication required')
    with db_session() as db:
        a=db.scalar(select(AuthSession).where(AuthSession.token_hash==_th(t),AuthSession.expires_at>datetime.now(timezone.utc)))
        if not a:raise HTTPException(401,'Session expired')
        u=db.get(User,a.user_id)
        if not u or not u.active:raise HTTPException(401,'User disabled')
        return _profile(u)
def require_admin(u):
    if u['role']!='admin':raise HTTPException(403,'Administrator role required')
    return u
def logout(t):
    if not t:return
    with db_session() as db:db.execute(delete(AuthSession).where(AuthSession.token_hash==_th(t)))
