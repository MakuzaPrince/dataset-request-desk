from collections.abc import Callable

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Role, User
from app.security import decode_token

_bearer = HTTPBearer(auto_error=False)


def _unauthorized(message: str) -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, message, headers={"WWW-Authenticate": "Bearer"})


def current_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    if creds is None:
        raise _unauthorized("Not authenticated")
    user_id = decode_token(creds.credentials)
    if user_id is None:
        raise _unauthorized("Invalid or expired token")
    # Loaded on every request so deactivation and role changes take effect immediately.
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise _unauthorized("Invalid or expired token")
    request.state.user_id = user.id
    return user


def require_roles(*roles: Role) -> Callable[..., User]:
    allowed = frozenset(roles)

    def dependency(user: User = Depends(current_user)) -> User:
        if user.role not in allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "You do not have permission to do this")
        return user

    return dependency


require_ops = require_roles(Role.operator, Role.admin)
require_admin = require_roles(Role.admin)
require_client = require_roles(Role.client)
