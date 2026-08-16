from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class ProjectEmbedding(Base):
    """One stored embedding vector per non-confidential project — the
    retrieval index behind Mode 3 (app/project_search.py).

    Deliberately NOT an Azure AI Search index. At 128 projects a brute-force
    scan is sub-millisecond, and keeping the vectors here means: no second
    index consumed out of the team's allocation, identical behaviour on
    SQLite and Azure SQL, and re-embedding a single edited description is
    one API call and one row update instead of an index push. Revisit at
    ~10k projects; the retrieval interface in project_search.py stays the
    same either way, only the storage behind it moves.

    Stored as raw little-endian float32 (array('f').tobytes()) rather than
    JSON: 6 KB per row instead of ~30 KB, and it round-trips through
    LargeBinary identically on both dialects (BLOB on SQLite,
    VARBINARY(max) on Azure SQL).

    Vectors are stored ALREADY L2-NORMALISED, so query-time cosine
    similarity is a plain dot product — see project_search._cosine.

    Confidential projects never get a row here at all. That's the same
    structural exclusion app/search_index.py's build_profile_text() applies
    to the employee corpus: the text never enters the corpus, so no
    query-time filter has to remember to exclude it. project_search.py
    filters again on the way out anyway, but that's defence in depth, not
    the mechanism.
    """

    __tablename__ = "project_embeddings"

    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), primary_key=True)

    # Which model produced this vector. A mixed-model table would be
    # silently wrong -- cosine between vectors from different embedding
    # models is meaningless -- so project_search.py refuses to rank across
    # more than one value here rather than returning confident nonsense.
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)

    # sha256 of the exact text that was embedded. This is what makes
    # re-embedding idempotent (skip rows whose text hasn't changed) and what
    # lets a stale row be detected after someone edits a project
    # description -- the "every write to an indexed field re-indexes" claim
    # in the README is enforceable for projects because of this column.
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    vector: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)

    project = relationship("Project")
