from sqlalchemy import Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class TrainingCourse(Base):
    """Catalogue of training courses the other team's system owns.

    This is a local mirror of *course identity only* — never of completion
    state, which always comes from a CertificationProvider (see
    app/certifications/). We hold a row per course so requirements can point
    at something stable and the UI has a name to render without a round trip.

    `code` is the external join key: the id the other team's API knows a
    course by, and the `course_id: str` in the provider interface. Internal
    integer PKs stay internal — nothing in app/certifications/ ever sends one
    over the wire, so if the other team's ids turn out to be UUIDs or slugs
    instead, only this column's contents change.
    """

    __tablename__ = "training_courses"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Retired courses stay in the table (existing status rows reference them)
    # but stop being expected of anyone.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
