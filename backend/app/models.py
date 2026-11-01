from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, ForeignKeyConstraint, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    # Режим входит в PK: синтетический ID не перезапишет настоящий профиль.
    mode: Mapped[str] = mapped_column(String(8), primary_key=True)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    first_name: Mapped[str | None] = mapped_column(String(200))
    last_name: Mapped[str | None] = mapped_column(String(200))
    sex: Mapped[int | None] = mapped_column(Integer)
    bdate: Mapped[str | None] = mapped_column(String(32))
    city_id: Mapped[int | None] = mapped_column(BigInteger)
    city: Mapped[str | None] = mapped_column(String(200))
    country: Mapped[str | None] = mapped_column(String(200))
    country_code: Mapped[str | None] = mapped_column(String(3))
    region: Mapped[str | None] = mapped_column(String(200))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Community(Base):
    __tablename__ = "communities"
    mode: Mapped[str] = mapped_column(String(8), primary_key=True)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(500))
    screen_name: Mapped[str | None] = mapped_column(String(200))
    members_count: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Search(Base):
    __tablename__ = "searches"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True)
    mode: Mapped[str] = mapped_column(String(8), index=True)
    query: Mapped[str] = mapped_column(String(200))
    normalized_query: Mapped[str] = mapped_column(String(200), index=True)
    modes: Mapped[list] = mapped_column(JSON)
    collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    details: Mapped[dict] = mapped_column(JSON, default=dict)


# Связи принадлежат последней выборке запроса. При её обновлении заменяются целиком.
class SearchUser(Base):
    __tablename__ = "search_users"
    search_id: Mapped[int] = mapped_column(ForeignKey("searches.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    mode: Mapped[str] = mapped_column(String(8))
    __table_args__ = (ForeignKeyConstraint(["mode", "user_id"], ["users.mode", "users.id"]),)


class Evidence(Base):
    __tablename__ = "evidence"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    search_id: Mapped[int] = mapped_column(ForeignKey("searches.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(String(16))
    source_id: Mapped[str] = mapped_column(String(100))
    __table_args__ = (
        UniqueConstraint("search_id", "user_id", "source", "source_id"),
        ForeignKeyConstraint(["search_id", "user_id"], ["search_users.search_id", "search_users.user_id"], ondelete="CASCADE"),
    )


class Membership(Base):
    __tablename__ = "memberships"
    search_id: Mapped[int] = mapped_column(ForeignKey("searches.id", ondelete="CASCADE"), primary_key=True)
    group_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    __table_args__ = (
        ForeignKeyConstraint(["search_id", "user_id"], ["search_users.search_id", "search_users.user_id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["search_id", "group_id"], ["search_communities.search_id", "search_communities.group_id"], ondelete="CASCADE"),
    )


class SearchCommunity(Base):
    __tablename__ = "search_communities"
    search_id: Mapped[int] = mapped_column(ForeignKey("searches.id", ondelete="CASCADE"), primary_key=True)
    group_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    mode: Mapped[str] = mapped_column(String(8))
    __table_args__ = (ForeignKeyConstraint(["mode", "group_id"], ["communities.mode", "communities.id"]),)
