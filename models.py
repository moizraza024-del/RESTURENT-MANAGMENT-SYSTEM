from datetime import datetime
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    phone1 = db.Column(db.String(20), nullable=True)
    phone2 = db.Column(db.String(20), nullable=True)
    country = db.Column(db.String(80), nullable=True)
    state = db.Column(db.String(80), nullable=True)
    city = db.Column(db.String(80), nullable=True)
    postal_code = db.Column(db.String(20), nullable=True)
    address1 = db.Column(db.String(255), nullable=True)
    address2 = db.Column(db.String(255), nullable=True)
    address3 = db.Column(db.String(255), nullable=True)
    role = db.Column(db.String(20), default='user')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    orders = db.relationship('Order', backref='user', lazy=True)
    custom_orders = db.relationship('CustomOrder', backref='user', lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    products = db.relationship('Product', backref='category', lazy=True)

class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text, nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'), nullable=True)
    image = db.Column(db.String(255), nullable=True)
    price = db.Column(db.Float, nullable=False, default=0.0)
    sale_price = db.Column(db.Float, nullable=False, default=0.0)
    making_cost = db.Column(db.Float, nullable=False, default=0.0)
    discount = db.Column(db.Float, default=0.0)
    stock = db.Column(db.Integer, nullable=False, default=0)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    addons = db.relationship('ProductAddOn', backref='product', lazy=True, cascade='all, delete-orphan')

    @property
    def price_after_discount(self):
        base_price = self.sale_price if self.sale_price is not None and self.sale_price > 0 else self.price
        return float(base_price) * (1 - float(self.discount) / 100)

    @property
    def profit_per_unit(self):
        return self.price_after_discount - float(self.making_cost)

class ProductAddOn(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    price = db.Column(db.Float, nullable=False)
    image = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    order_number = db.Column(db.String(64), unique=True, nullable=True)
    order_name = db.Column(db.String(150), nullable=True)
    payment_method = db.Column(db.String(50), nullable=True, default='Cash on Delivery')
    payment_details = db.Column(db.Text, nullable=True)
    payment_proof = db.Column(db.String(255), nullable=True)
    amount_paid = db.Column(db.Float, nullable=False, default=0.0)
    total_price = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(50), default='Under Process')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    items = db.relationship('OrderItem', backref='order', lazy=True)
    addons = db.relationship('AddOn', backref='order', lazy=True)

class OrderItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('order.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    price = db.Column(db.Float, nullable=False)
    making_cost = db.Column(db.Float, nullable=False, default=0.0)
    product = db.relationship('Product')

    @property
    def profit(self):
        return (self.price - self.making_cost) * self.quantity

class CustomOrder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    custom_order_number = db.Column(db.String(64), unique=True, nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    title = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text, nullable=False)
    uploaded_image = db.Column(db.String(255), nullable=True)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    estimate_price = db.Column(db.Float, nullable=True)
    status = db.Column(db.String(50), default='Pending')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class PromotionBanner(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    image = db.Column(db.String(255), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'), nullable=True)
    active = db.Column(db.Boolean, default=True)

class Announcement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(120), nullable=False)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class AddOn(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('order.id'), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    price = db.Column(db.Float, nullable=False)


class ContactMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(30), nullable=False)
    message = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(50), default='New')
    response = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SearchQuery(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    query_text = db.Column('query', db.String(255), nullable=False, index=True)
    count = db.Column(db.Integer, nullable=False, default=1)
    last_searched = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class SiteSetting(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.String(255), nullable=True)

