from __future__ import annotations

import json
import hmac
import logging
import os
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from threading import Lock
from typing import Annotated

import jwt
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer
from google import genai
from google.genai import errors as genai_errors
from passlib.context import CryptContext
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, create_engine, func, inspect, select, text, update
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("campusbite")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./campusbite.db")
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {})
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
JWT_SECRET = os.getenv("JWT_SECRET", "local-development-secret-change-me")
JWT_ALGORITHM = "HS256"
TOKEN_MINUTES = int(os.getenv("TOKEN_EXPIRE_MINUTES", "1440"))
APP_ENV = os.getenv("APP_ENV", "development").lower()
_rate_limit_lock = Lock()
_request_times: dict[str, deque[float]] = defaultdict(deque)
password_hash = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


class Base(DeclarativeBase): pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(30), default="customer")
    phone: Mapped[str] = mapped_column(String(40), default="")
    address: Mapped[str] = mapped_column(String(500), default="")


class Restaurant(Base):
    __tablename__ = "restaurants"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    cuisine: Mapped[str] = mapped_column(String(100), default="Campus kitchen")
    image: Mapped[str] = mapped_column(String(500), default="")
    rating: Mapped[float] = mapped_column(Float, default=4.7)
    eta: Mapped[str] = mapped_column(String(30), default="20–30 min")
    items: Mapped[list[MenuItem]] = relationship(back_populates="restaurant", cascade="all, delete-orphan")


class MenuItem(Base):
    __tablename__ = "menu_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    restaurant_id: Mapped[int] = mapped_column(ForeignKey("restaurants.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(80), default="Mains")
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    image: Mapped[str] = mapped_column(String(500), default="")
    available: Mapped[bool] = mapped_column(Boolean, default=True)
    restaurant: Mapped[Restaurant] = relationship(back_populates="items")


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    restaurant_id: Mapped[int] = mapped_column(ForeignKey("restaurants.id"), index=True)
    delivery_partner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="PLACED")
    payment_status: Mapped[str] = mapped_column(String(30), default="mock_paid")
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    delivery_address: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    items: Mapped[list[OrderItem]] = relationship(cascade="all, delete-orphan")
    restaurant: Mapped[Restaurant] = relationship()
    customer: Mapped[User] = relationship(foreign_keys=[customer_id])
    delivery_partner: Mapped[User] = relationship(foreign_keys=[delivery_partner_id])
    return_request: Mapped[ReturnRequest] = relationship(uselist=False)
    review: Mapped[Review] = relationship(uselist=False)


class OrderItem(Base):
    __tablename__ = "order_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    menu_item_id: Mapped[int] = mapped_column(ForeignKey("menu_items.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(160))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))


class Ledger(Base):
    __tablename__ = "payment_ledger"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    type: Mapped[str] = mapped_column(String(30))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    status: Mapped[str] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Review(Base):
    __tablename__ = "reviews"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), unique=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    rating: Mapped[int] = mapped_column(Integer)
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class ReturnRequest(Base):
    __tablename__ = "return_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), unique=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Reminder(Base):
    __tablename__ = "reminders"
    id: Mapped[int] = mapped_column(primary_key=True)
    restaurant_id: Mapped[int] = mapped_column(ForeignKey("restaurants.id"))
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    message: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class RegisterIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=3, max_length=255, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=8)
    role: str = "customer"
    invitation_code: str = Field(default="", max_length=256)
    phone: str = Field(default="", max_length=40)
    address: str = Field(default="", max_length=500)


class LoginIn(BaseModel): email: str = Field(min_length=3, max_length=255); password: str = Field(min_length=1, max_length=128)
class MenuIn(BaseModel): name: str = Field(min_length=2, max_length=160); description: str = Field(default="", max_length=2000); category: str = Field(default="Mains", max_length=80); price: Decimal = Field(gt=0, max_digits=10, decimal_places=2); image: str = Field(default="", max_length=500); available: bool = True
class CartLine(BaseModel): menu_item_id: int; quantity: int = Field(gt=0, le=50)
class CheckoutIn(BaseModel): restaurant_id: int; items: list[CartLine] = Field(min_length=1, max_length=50); delivery_address: str = Field(default="", max_length=500)
class StatusIn(BaseModel): status: str = Field(min_length=1, max_length=30)
class AssignIn(BaseModel): delivery_partner_id: int = Field(gt=0)
class ReviewIn(BaseModel): rating: int = Field(ge=1, le=5); comment: str = Field(default="", max_length=2000)
class ReturnIn(BaseModel): reason: str = Field(min_length=5, max_length=1500)
class DecisionIn(BaseModel): approve: bool
class InsightIn(BaseModel): question: str = Field(min_length=2, max_length=1000)


def db_session():
    db = SessionLocal()
    try: yield db
    finally: db.close()


def user_payload(user: User): return {"id": user.id, "name": user.name, "email": user.email, "role": user.role, "phone": user.phone, "address": user.address}
def token_for(user: User): return jwt.encode({"sub": str(user.id), "exp": datetime.now(timezone.utc) + timedelta(minutes=TOKEN_MINUTES)}, JWT_SECRET, algorithm=JWT_ALGORITHM)


def current_user(token: Annotated[str, Depends(oauth2_scheme)], db: Annotated[Session, Depends(db_session)]):
    try: user_id = int(jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])["sub"])
    except Exception: raise HTTPException(401, "Invalid or expired token", headers={"WWW-Authenticate": "Bearer"})
    user = db.get(User, user_id)
    if not user: raise HTTPException(401, "Account not found")
    return user


def require_role(*roles):
    def check(user: Annotated[User, Depends(current_user)]):
        if user.role not in roles: raise HTTPException(403, "You do not have access to this action")
        return user
    return check


def enforce_rate_limit(key: str, *, limit: int, window_seconds: int = 60):
    """Per-process sliding window; use a shared store when running multiple workers."""
    now = time.monotonic()
    with _rate_limit_lock:
        events = _request_times[key]
        while events and now - events[0] >= window_seconds:
            events.popleft()
        if len(events) >= limit:
            retry_after = max(1, int(window_seconds - (now - events[0])))
            raise HTTPException(429, "Too many requests. Please wait and try again.", headers={"Retry-After": str(retry_after)})
        events.append(now)
        if len(_request_times) > 5000:
            expired = [bucket for bucket, values in _request_times.items() if not values or now - values[-1] >= window_seconds]
            for bucket in expired:
                _request_times.pop(bucket, None)


def validate_runtime_config():
    if APP_ENV == "production":
        if len(JWT_SECRET) < 32 or JWT_SECRET == "local-development-secret-change-me":
            raise RuntimeError("Set a unique JWT_SECRET with at least 32 characters for production")
        if "*" in os.getenv("CORS_ORIGINS", ""):
            raise RuntimeError("CORS_ORIGINS must list explicit trusted origins in production")


def order_dict(o: Order):
    return {"id": o.id, "status": o.status, "payment_status": o.payment_status, "total": o.total, "delivery_address": o.delivery_address, "created_at": o.created_at.isoformat(), "updated_at": o.updated_at.isoformat(), "restaurant": {"id": o.restaurant.id, "name": o.restaurant.name}, "customer": {"name": o.customer.name, "phone": o.customer.phone}, "delivery_partner": user_payload(o.delivery_partner) if o.delivery_partner else None, "return_request": ({"status": o.return_request.status, "reason": o.return_request.reason} if o.return_request else None), "review": ({"rating": o.review.rating, "comment": o.review.comment} if o.review else None), "items": [{"name": i.name, "quantity": i.quantity, "unit_price": i.unit_price} for i in o.items]}


def restaurant_ratings(db: Session):
    rows = db.execute(select(Order.restaurant_id, func.avg(Review.rating), func.count(Review.id)).join(Review, Review.order_id == Order.id).group_by(Order.restaurant_id)).all()
    return {restaurant_id: {"rating": round(float(average), 1), "review_count": count} for restaurant_id, average, count in rows}


def seed():
    Base.metadata.create_all(engine)
    migrate_legacy_money_columns()
    with SessionLocal() as db:
        if db.scalar(select(Restaurant.id).limit(1)): return
        restaurants = [
            Restaurant(name="The Daily Grind", cuisine="Coffee · Brunch", description="Your favorite campus cafe, made for slow mornings and good company.", image="https://images.unsplash.com/photo-1554118811-1e0d58224f24?auto=format&fit=crop&w=1000&q=85", rating=4.8, eta="15–25 min"),
            Restaurant(name="Nori House", cuisine="Japanese · Bowls", description="Freshly rolled, thoughtfully made, always worth the walk.", image="https://images.unsplash.com/photo-1579871494447-9811cf80d66c?auto=format&fit=crop&w=1000&q=85", rating=4.9, eta="20–30 min"),
            Restaurant(name="Little Napoli", cuisine="Italian · Pizza", description="Wood-fired comfort with a little Italian sunshine.", image="https://images.unsplash.com/photo-1579751626657-72bc17010498?auto=format&fit=crop&w=1000&q=85", rating=4.7, eta="25–35 min"),
            Restaurant(name="Greenhouse", cuisine="Healthy · Smoothies", description="Bright bowls, crisp greens and smoothies for the in-between.", image="https://images.unsplash.com/photo-1540420773420-3366772f4999?auto=format&fit=crop&w=1000&q=85", rating=4.6, eta="10–20 min"),
        ]
        db.add_all(restaurants); db.flush()
        menu = [[("Honey oat latte", "Espresso, oat milk and a little wildflower honey.", "Coffee", 5.5), ("Ricotta toast", "Whipped ricotta, stone fruit and sourdough.", "Brunch", 9.0), ("Cinnamon roll", "Soft, buttery and finished with vanilla glaze.", "Bakery", 4.5)], [("Salmon crunch bowl", "Crispy rice, salmon, avocado and sesame.", "Bowls", 13.5), ("Miso aubergine roll", "Sweet miso, cucumber and toasted sesame.", "Sushi", 8.5), ("Matcha mochi", "Two soft rice cakes with ceremonial matcha.", "Dessert", 5.0)], [("Margherita", "San Marzano tomato, fior di latte, basil.", "Pizza", 12.0), ("Truffle mushroom", "Wild mushrooms, pecorino and truffle oil.", "Pizza", 15.0), ("Tiramisu cup", "Espresso-soaked sponge, mascarpone, cocoa.", "Dessert", 6.5)], [("Green goddess bowl", "Avocado, quinoa, cucumber and tahini.", "Bowls", 12.5), ("Mango sunshine", "Mango, banana, coconut and lime.", "Smoothies", 7.0), ("Crispy chickpea wrap", "Herby yogurt, crunchy greens, warm flatbread.", "Wraps", 9.5)]]
        for restaurant, entries in zip(restaurants, menu):
            for name, desc, category, price in entries: db.add(MenuItem(restaurant_id=restaurant.id, name=name, description=desc, category=category, price=price))
        db.commit()


def migrate_legacy_money_columns():
    """Convert legacy PostgreSQL float money columns to fixed precision without dropping data."""
    if engine.dialect.name != "postgresql":
        return
    money_columns = {
        "menu_items": ("price",),
        "orders": ("total",),
        "order_items": ("unit_price",),
        "payment_ledger": ("amount",),
    }
    inspector = inspect(engine)
    for table, names in money_columns.items():
        existing = {column["name"]: column["type"] for column in inspector.get_columns(table)}
        for column in names:
            current_type = existing.get(column)
            if current_type is None or (isinstance(current_type, Numeric) and current_type.precision == 12 and current_type.scale == 2):
                continue
            with engine.begin() as connection:
                connection.execute(text(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE NUMERIC(12, 2) USING ROUND({column}::numeric, 2)"))


app = FastAPI(title="CampusBite API", version="1.0.0", description="CampusBite restaurant ordering and delivery API")
allowed_origins = [origin.strip() for origin in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",") if origin.strip()]
if APP_ENV != "production":
    allowed_origins = sorted(set(allowed_origins) | {f"http://localhost:{port}" for port in range(5173, 5181)})
app.add_middleware(CORSMiddleware, allow_origins=allowed_origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def request_logging(request, call_next):
    started = time.perf_counter(); response = await call_next(request); elapsed = (time.perf_counter() - started) * 1000
    log.info("%s %s %s %.1fms", request.method, request.url.path, response.status_code, elapsed); response.headers["X-Process-Time-ms"] = f"{elapsed:.1f}"; return response


@app.on_event("startup")
def on_startup():
    validate_runtime_config()
    seed()


@app.get("/api/health")
def health(): return {"status": "ok", "service": "CampusBite"}


@app.post("/api/auth/register", status_code=201)
def register(data: RegisterIn, db: Annotated[Session, Depends(db_session)]):
    if data.role not in {"customer", "owner", "delivery"}: raise HTTPException(400, "Role must be customer, owner or delivery")
    if data.role in {"owner", "delivery"}:
        code_name = "OWNER_REGISTRATION_CODE" if data.role == "owner" else "DELIVERY_REGISTRATION_CODE"
        configured_code = os.getenv(code_name, "")
        if not configured_code or not hmac.compare_digest(data.invitation_code, configured_code):
            raise HTTPException(403, f"A valid {data.role} invitation code is required")
    if len(data.password.encode("utf-8")) > 72: raise HTTPException(422, "Password must be at most 72 UTF-8 bytes")
    email = data.email.strip().lower()
    if db.scalar(select(User).where(User.email == email)): raise HTTPException(409, "Email is already registered")
    user = User(name=data.name, email=email, password_hash=password_hash.hash(data.password), role=data.role, phone=data.phone, address=data.address)
    db.add(user); db.commit(); db.refresh(user)
    if user.role == "owner":
        db.add(Restaurant(owner_id=user.id, name=f"{user.name}'s Kitchen", cuisine="Campus kitchen", description="Your restaurant on CampusBite.")); db.commit()
    return {"access_token": token_for(user), "token_type": "bearer", "user": user_payload(user)}


@app.post("/api/auth/login")
def login(data: LoginIn, request: Request, db: Annotated[Session, Depends(db_session)]):
    enforce_rate_limit(f"login:{request.client.host if request.client else 'unknown'}", limit=10)
    if len(data.password.encode("utf-8")) > 72: raise HTTPException(422, "Password must be at most 72 UTF-8 bytes")
    user = db.scalar(select(User).where(User.email == data.email.strip().lower()))
    if not user or not password_hash.verify(data.password, user.password_hash): raise HTTPException(401, "Email or password is incorrect")
    return {"access_token": token_for(user), "token_type": "bearer", "user": user_payload(user)}


@app.get("/api/auth/me")
def me(user: Annotated[User, Depends(current_user)]): return user_payload(user)


@app.get("/api/restaurants")
def restaurants(db: Annotated[Session, Depends(db_session)], search: str = ""):
    query = select(Restaurant)
    if search: query = query.where(Restaurant.name.ilike(f"%{search}%") | Restaurant.cuisine.ilike(f"%{search}%"))
    ratings = restaurant_ratings(db)
    return [{"id": r.id, "name": r.name, "cuisine": r.cuisine, "description": r.description, "image": r.image, **ratings.get(r.id, {"rating": None, "review_count": 0}), "eta": r.eta, "accepting_orders": r.owner_id is not None, "menu_count": len([i for i in r.items if i.available]), "categories": sorted({i.category.lower() for i in r.items if i.available})} for r in db.scalars(query).all()]


@app.get("/api/restaurants/{restaurant_id}")
def restaurant_detail(restaurant_id: int, db: Annotated[Session, Depends(db_session)], search: str = "", min_price: float | None = None, max_price: float | None = None, page: int = Query(1, ge=1), page_size: int = Query(30, ge=1, le=100)):
    r = db.get(Restaurant, restaurant_id)
    if not r: raise HTTPException(404, "Restaurant not found")
    query = select(MenuItem).where(MenuItem.restaurant_id == restaurant_id, MenuItem.available.is_(True))
    if search: query = query.where(MenuItem.name.ilike(f"%{search}%"))
    if min_price is not None: query = query.where(MenuItem.price >= min_price)
    if max_price is not None: query = query.where(MenuItem.price <= max_price)
    total = db.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
    query = query.offset((page - 1) * page_size).limit(page_size)
    rating = restaurant_ratings(db).get(r.id, {"rating": None, "review_count": 0})
    return {"id": r.id, "name": r.name, "cuisine": r.cuisine, "description": r.description, "image": r.image, **rating, "eta": r.eta, "accepting_orders": r.owner_id is not None, "items": [{"id": i.id, "name": i.name, "description": i.description, "category": i.category, "price": i.price, "image": i.image, "available": i.available} for i in db.scalars(query).all()], "page": page, "page_size": page_size, "total": total}


def owner_restaurant(db: Session, user: User, restaurant_id: int | None = None):
    r = db.scalar(select(Restaurant).where(Restaurant.owner_id == user.id, *([Restaurant.id == restaurant_id] if restaurant_id else [])))
    if not r: raise HTTPException(404, "Restaurant not found for this owner")
    return r


@app.get("/api/owner/claimable-restaurants")
def claimable_restaurants(user: Annotated[User, Depends(require_role("owner"))], db: Annotated[Session, Depends(db_session)]):
    return [{"id": r.id, "name": r.name, "cuisine": r.cuisine} for r in db.scalars(select(Restaurant).where(Restaurant.owner_id.is_(None)).order_by(Restaurant.name)).all()]


@app.post("/api/owner/claimable-restaurants/{restaurant_id}")
def claim_restaurant(restaurant_id: int, user: Annotated[User, Depends(require_role("owner"))], db: Annotated[Session, Depends(db_session)]):
    current = db.scalars(select(Restaurant).where(Restaurant.owner_id == user.id)).all()
    if current:
        if len(current) != 1 or current[0].name != f"{user.name}'s Kitchen" or current[0].items:
            raise HTTPException(409, "This account already has a restaurant in use and cannot claim a sample restaurant")
        if db.scalar(select(Order.id).where(Order.restaurant_id == current[0].id).limit(1)):
            raise HTTPException(409, "This account already has restaurant orders and cannot claim a sample restaurant")
    try:
        result = db.execute(update(Restaurant).where(Restaurant.id == restaurant_id, Restaurant.owner_id.is_(None)).values(owner_id=user.id))
        if result.rowcount != 1:
            db.rollback()
            raise HTTPException(409, "That restaurant has already been claimed")
        if current:
            db.delete(current[0])
        db.commit()
    except HTTPException:
        raise
    except Exception:
        db.rollback()
        raise
    return {"id": restaurant_id, "message": "Restaurant claimed successfully"}


@app.post("/api/owner/menu", status_code=201)
def create_menu(data: MenuIn, user: Annotated[User, Depends(require_role("owner"))], db: Annotated[Session, Depends(db_session)]):
    r = owner_restaurant(db, user); item = MenuItem(restaurant_id=r.id, **data.model_dump()); db.add(item); db.commit(); db.refresh(item); return {"id": item.id, **data.model_dump()}


@app.get("/api/owner/menu")
def owner_menu(user: Annotated[User, Depends(require_role("owner"))], db: Annotated[Session, Depends(db_session)]):
    r = owner_restaurant(db, user)
    return [{"id": i.id, "name": i.name, "description": i.description, "category": i.category, "price": i.price, "image": i.image, "available": i.available} for i in db.scalars(select(MenuItem).where(MenuItem.restaurant_id == r.id).order_by(MenuItem.category, MenuItem.name)).all()]


@app.patch("/api/owner/menu/{item_id}")
def update_menu(item_id: int, data: MenuIn, user: Annotated[User, Depends(require_role("owner"))], db: Annotated[Session, Depends(db_session)]):
    item = db.get(MenuItem, item_id)
    if not item or item.restaurant.owner_id != user.id: raise HTTPException(404, "Menu item not found")
    for key, value in data.model_dump().items(): setattr(item, key, value)
    db.commit(); return {"id": item.id, **data.model_dump()}


@app.delete("/api/owner/menu/{item_id}", status_code=204)
def delete_menu(item_id: int, user: Annotated[User, Depends(require_role("owner"))], db: Annotated[Session, Depends(db_session)]):
    item = db.get(MenuItem, item_id)
    if not item or item.restaurant.owner_id != user.id: raise HTTPException(404, "Menu item not found")
    # Keep order snapshots intact while removing the live catalog reference.
    db.execute(update(OrderItem).where(OrderItem.menu_item_id == item.id).values(menu_item_id=None))
    db.delete(item); db.commit()
    return Response(status_code=204)


@app.post("/api/orders", status_code=201)
def checkout(data: CheckoutIn, user: Annotated[User, Depends(require_role("customer"))], db: Annotated[Session, Depends(db_session)]):
    if not data.items: raise HTTPException(400, "Your cart is empty")
    delivery_address = data.delivery_address.strip() or user.address.strip()
    if len(delivery_address) < 5: raise HTTPException(400, "A delivery address is required")
    restaurant = db.get(Restaurant, data.restaurant_id)
    if not restaurant: raise HTTPException(404, "Restaurant not found")
    if restaurant.owner_id is None: raise HTTPException(409, "This sample restaurant is not accepting orders until an owner claims it")
    ids = [line.menu_item_id for line in data.items]
    products = {i.id: i for i in db.scalars(select(MenuItem).where(MenuItem.id.in_(ids), MenuItem.restaurant_id == restaurant.id)).all()}
    if len(products) != len(set(ids)): raise HTTPException(400, "One or more items do not belong to this restaurant")
    if any(not products[line.menu_item_id].available for line in data.items): raise HTTPException(400, "A cart item is no longer available")
    total = sum((Decimal(str(products[x.menu_item_id].price)) * x.quantity for x in data.items), Decimal("0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    order = Order(customer_id=user.id, restaurant_id=restaurant.id, total=total, delivery_address=delivery_address); db.add(order); db.flush()
    for line in data.items:
        product = products[line.menu_item_id]; db.add(OrderItem(order_id=order.id, menu_item_id=product.id, name=product.name, quantity=line.quantity, unit_price=product.price))
    db.add(Ledger(order_id=order.id, type="payment", amount=total, status="mock_paid")); db.commit(); db.refresh(order); return order_dict(order)


@app.get("/api/orders")
def list_orders(user: Annotated[User, Depends(current_user)], db: Annotated[Session, Depends(db_session)], status_filter: str = "", date_from: str = "", date_to: str = "", page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    query = select(Order).order_by(Order.created_at.desc())
    if user.role == "customer": query = query.where(Order.customer_id == user.id)
    elif user.role == "owner": query = query.where(Order.restaurant_id == owner_restaurant(db, user).id)
    else: query = query.where(Order.delivery_partner_id == user.id)
    if status_filter: query = query.where(Order.status == status_filter.upper())
    if date_from:
        try: query = query.where(Order.created_at >= datetime.fromisoformat(date_from).replace(tzinfo=timezone.utc))
        except ValueError: raise HTTPException(422, "date_from must be an ISO date")
    if date_to:
        try: query = query.where(Order.created_at < datetime.fromisoformat(date_to).replace(tzinfo=timezone.utc) + timedelta(days=1))
        except ValueError: raise HTTPException(422, "date_to must be an ISO date")
    total = db.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
    orders = db.scalars(query.offset((page - 1) * page_size).limit(page_size)).all()
    return {"items": [order_dict(o) for o in orders], "page": page, "page_size": page_size, "total": total}


@app.get("/api/orders/{order_id}")
def order_detail(order_id: int, user: Annotated[User, Depends(current_user)], db: Annotated[Session, Depends(db_session)]):
    order = db.get(Order, order_id)
    if not order: raise HTTPException(404, "Order not found")
    if not (user.id == order.customer_id or user.id == order.delivery_partner_id or (user.role == "owner" and order.restaurant.owner_id == user.id)): raise HTTPException(403, "You cannot view this order")
    return order_dict(order)


@app.patch("/api/orders/{order_id}/status")
def change_status(order_id: int, data: StatusIn, user: Annotated[User, Depends(current_user)], db: Annotated[Session, Depends(db_session)]):
    order = db.get(Order, order_id)
    if not order: raise HTTPException(404, "Order not found")
    target = data.status.upper(); allowed = {"owner": {"PLACED": {"ACCEPTED"}, "ACCEPTED": {"PREPARING"}, "PREPARING": {"READY"}}, "delivery": {"ASSIGNED": {"OUT_FOR_DELIVERY"}, "OUT_FOR_DELIVERY": {"DELIVERED"}}}
    if user.role == "customer" and order.customer_id == user.id and order.status == "PLACED" and target == "CANCELLED": order.status = target
    elif user.role == "owner" and order.restaurant.owner_id == user.id and target in allowed["owner"].get(order.status, set()): order.status = target
    elif user.role == "delivery" and order.delivery_partner_id == user.id and target in allowed["delivery"].get(order.status, set()): order.status = target
    else: raise HTTPException(400, f"Invalid status transition from {order.status} to {target}")
    order.updated_at = datetime.now(timezone.utc); db.commit(); db.refresh(order); return order_dict(order)


@app.get("/api/delivery/partners")
def delivery_partners(user: Annotated[User, Depends(require_role("owner"))], db: Annotated[Session, Depends(db_session)]):
    return [{"id": u.id, "name": u.name, "phone": u.phone} for u in db.scalars(select(User).where(User.role == "delivery").order_by(User.name)).all()]


@app.patch("/api/orders/{order_id}/assign")
def assign_delivery(order_id: int, data: AssignIn, user: Annotated[User, Depends(require_role("owner"))], db: Annotated[Session, Depends(db_session)]):
    order = db.get(Order, order_id); partner = db.get(User, data.delivery_partner_id)
    if not order or order.restaurant.owner_id != user.id: raise HTTPException(404, "Order not found")
    if order.status != "READY": raise HTTPException(400, "Orders can only be assigned when READY")
    if not partner or partner.role != "delivery": raise HTTPException(400, "Select a valid delivery partner")
    order.delivery_partner_id = partner.id; order.status = "ASSIGNED"; order.updated_at = datetime.now(timezone.utc); db.commit(); return order_dict(order)


@app.post("/api/orders/{order_id}/review", status_code=201)
def review_order(order_id: int, data: ReviewIn, user: Annotated[User, Depends(require_role("customer"))], db: Annotated[Session, Depends(db_session)]):
    order = db.get(Order, order_id)
    if not order or order.customer_id != user.id: raise HTTPException(404, "Order not found")
    if order.status != "DELIVERED": raise HTTPException(400, "Reviews are available after delivery")
    if db.scalar(select(Review).where(Review.order_id == order_id)): raise HTTPException(409, "This order already has a review")
    review = Review(order_id=order_id, customer_id=user.id, **data.model_dump()); db.add(review); db.commit(); return {"id": review.id, "order_id": order_id, **data.model_dump()}


@app.post("/api/orders/{order_id}/returns", status_code=201)
def request_return(order_id: int, data: ReturnIn, user: Annotated[User, Depends(require_role("customer"))], db: Annotated[Session, Depends(db_session)]):
    order = db.get(Order, order_id)
    if not order or order.customer_id != user.id: raise HTTPException(404, "Order not found")
    if order.status != "DELIVERED": raise HTTPException(400, "Returns are available after delivery")
    if db.scalar(select(ReturnRequest).where(ReturnRequest.order_id == order_id)): raise HTTPException(409, "A return request already exists")
    item = ReturnRequest(order_id=order_id, customer_id=user.id, reason=data.reason); db.add(item); db.commit(); return {"id": item.id, "status": item.status, "reason": item.reason}


@app.get("/api/owner/returns")
def owner_returns(user: Annotated[User, Depends(require_role("owner"))], db: Annotated[Session, Depends(db_session)]):
    r = owner_restaurant(db, user); rows = db.scalars(select(ReturnRequest).join(Order).where(Order.restaurant_id == r.id).order_by(ReturnRequest.created_at.desc())).all()
    return [{"id": x.id, "order_id": x.order_id, "reason": x.reason, "status": x.status, "total": db.get(Order, x.order_id).total} for x in rows]


@app.patch("/api/owner/returns/{request_id}")
def decide_return(request_id: int, data: DecisionIn, user: Annotated[User, Depends(require_role("owner"))], db: Annotated[Session, Depends(db_session)]):
    request = db.get(ReturnRequest, request_id)
    if not request or db.get(Order, request.order_id).restaurant.owner_id != user.id: raise HTTPException(404, "Return request not found")
    if request.status != "pending": raise HTTPException(400, "This return request has already been decided")
    order = db.get(Order, request.order_id); request.status = "approved" if data.approve else "rejected"
    if data.approve:
        order.payment_status = "refunded"; order.status = "REFUNDED"; db.add(Ledger(order_id=order.id, type="refund", amount=order.total, status="refunded"))
    db.commit(); return {"id": request.id, "order_id": order.id, "status": request.status}


def make_reminders(restaurant_id: int):
    with SessionLocal() as db:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=15)
        orders = db.scalars(select(Order).where(Order.restaurant_id == restaurant_id, Order.status.in_(["PLACED", "ACCEPTED"]), Order.updated_at < cutoff)).all()
        existing = set(db.scalars(select(Reminder.order_id).where(Reminder.restaurant_id == restaurant_id)).all())
        for order in orders:
            if order.id not in existing: db.add(Reminder(restaurant_id=restaurant_id, order_id=order.id, message=f"Order #{order.id} has been waiting for over 15 minutes."))
        db.commit()


@app.get("/api/owner/dashboard")
def owner_dashboard(user: Annotated[User, Depends(require_role("owner"))], db: Annotated[Session, Depends(db_session)]):
    r = owner_restaurant(db, user); make_reminders(r.id); orders = db.scalars(select(Order).where(Order.restaurant_id == r.id).order_by(Order.created_at.desc()).limit(50)).all()
    reminders = db.scalars(select(Reminder).where(Reminder.restaurant_id == r.id).order_by(Reminder.created_at.desc()).limit(20)).all()
    claimable = db.scalars(select(Restaurant).where(Restaurant.owner_id.is_(None)).order_by(Restaurant.name)).all()
    today = datetime.now(timezone.utc).date()
    stats = {"orders": db.scalar(select(func.count(Order.id)).where(Order.restaurant_id == r.id, func.date(Order.created_at) == today)) or 0, "active": db.scalar(select(func.count(Order.id)).where(Order.restaurant_id == r.id, Order.status.not_in(["DELIVERED", "CANCELLED", "REFUNDED"]))) or 0, "revenue": round(db.scalar(select(func.coalesce(func.sum(Order.total), 0)).where(Order.restaurant_id == r.id, Order.payment_status == "mock_paid")) or 0, 2)}
    return {"restaurant": {"id": r.id, "name": r.name}, "claimable_restaurants": [{"id": x.id, "name": x.name, "cuisine": x.cuisine} for x in claimable], "stats": stats, "orders": [order_dict(o) for o in orders], "reminders": [{"id": x.id, "order_id": x.order_id, "message": x.message, "created_at": x.created_at.isoformat()} for x in reminders]}


@app.post("/api/insights")
def insights(data: InsightIn, user: Annotated[User, Depends(require_role("customer"))], db: Annotated[Session, Depends(db_session)]):
    enforce_rate_limit(f"insights:{user.id}", limit=int(os.getenv("GEMINI_REQUESTS_PER_MINUTE", "5")))
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key: raise HTTPException(503, "Order insights are unavailable until GEMINI_API_KEY is configured")
    rows = db.scalars(select(Order).where(Order.customer_id == user.id).order_by(Order.created_at.desc()).limit(100)).all()
    context = [{"id": o.id, "restaurant": o.restaurant.name, "status": o.status, "total": float(o.total), "date": o.created_at.isoformat(), "items": [{"name": i.name, "quantity": i.quantity} for i in o.items]} for o in rows]
    try:
        client = genai.Client(
            api_key=api_key,
            http_options=genai.types.HttpOptions(
                timeout=30000,
                retry_options=genai.types.HttpRetryOptions(attempts=1),
            ),
        )
        response = client.models.generate_content(
            model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
            contents=f"Customer order history (newest first):\n{json.dumps(context, ensure_ascii=False)}\n\nCustomer question: {data.question}",
            config=genai.types.GenerateContentConfig(
                system_instruction="Answer using only this authenticated customer's order history. Do not invent orders or facts. If the history is empty or does not answer the question, say so. Never infer or reveal another customer's information.",
                temperature=0.2,
                max_output_tokens=512,
            ),
        )
        answer = (response.text or "").strip()
        if not answer: raise HTTPException(502, "Gemini returned an empty response. Please try again.")
        return {"answer": answer}
    except HTTPException:
        raise
    except genai_errors.APIError as exc:
        if exc.code == 429:
            raise HTTPException(429, "Gemini's free-tier quota or rate limit was reached. Please wait and try again later.")
        if exc.code == 401:
            raise HTTPException(503, "Gemini rejected the API key. Check GEMINI_API_KEY in the backend environment.")
        if exc.code == 503:
            raise HTTPException(503, "Gemini is temporarily busy. Please try again shortly.")
        if exc.code == 404:
            raise HTTPException(503, "The configured Gemini model is unavailable. Check GEMINI_MODEL in the backend environment.")
        log.error("Gemini insights request failed with status %s", exc.code)
        raise HTTPException(502, "Could not generate insights right now. Please try again later.")
    except Exception:
        log.exception("Gemini insights request failed"); raise HTTPException(502, "Could not generate insights right now. Please try again later.")
