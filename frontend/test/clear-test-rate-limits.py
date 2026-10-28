import os
import sys

backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app.db.session import get_session_factory
from app.modules.authentication.models import OtpRateBucket
from sqlalchemy import delete

try:
    with get_session_factory()() as session:
        session.execute(delete(OtpRateBucket).where(OtpRateBucket.mobile_e164 == "+15550000001"))
        session.commit()
except Exception as e:
    print("Failed to reset rate limits:", e, file=sys.stderr)

