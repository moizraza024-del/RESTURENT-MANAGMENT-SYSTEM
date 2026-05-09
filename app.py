import os
import io
import random
from datetime import datetime, timedelta
from functools import wraps
from flask import (Flask, render_template, redirect, url_for, request, flash,
                   session, abort, send_file, send_from_directory, jsonify)
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.utils import secure_filename
from sqlalchemy import text
from matplotlib.figure import Figure
try:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import inch
    from reportlab.lib.utils import ImageReader
    from reportlab.lib import colors
    HAS_REPORTLAB = True
except ImportError:
    HAS_REPORTLAB = False

from models import db, User, Category, Product, ProductAddOn, Order, OrderItem, CustomOrder, PromotionBanner, Announcement, AddOn, SiteSetting, ContactMessage, SearchQuery

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads')
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

app = Flask(__name__)
app.config['SECRET_KEY'] = 'super-secret-key-123'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(BASE_DIR, 'bakery.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

app.jinja_env.globals.update(enumerate=enumerate)

db.init_app(app)

login_manager = LoginManager(app)
login_manager.login_view = 'login'

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'admin':
            flash('Admin access required', 'danger')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


def generate_order_number():
    return f"SB{datetime.utcnow().strftime('%Y%m%d%H%M%S')}{random.randint(1000,9999)}"


def get_user_complete_due(user_id):
    return db.session.query(
        db.func.coalesce(db.func.sum(Order.total_price - Order.amount_paid), 0.0)
    ).filter(Order.user_id == user_id, Order.status == 'Complete').scalar() or 0.0


def ensure_user_due_warning(user):
    due_threshold_setting = SiteSetting.query.filter_by(key='due_warning_threshold').first()
    due_threshold = float(due_threshold_setting.value) if due_threshold_setting and due_threshold_setting.value else 0.0
    due = get_user_complete_due(user.id)

    warning_msg = None
    warning_status = None

    if due_threshold > 0 and due >= due_threshold:
        warning_msg = f"Your completed orders have outstanding due PKR {due:.2f}, exceeding threshold PKR {due_threshold:.2f}. Please pay immediately."
        warning_status = 'Warning Sent'
    else:
        warning_msg = f"Your completed orders outstanding due is PKR {due:.2f}."
        warning_status = 'Warning Cleared'

    if warning_msg and warning_status:
        msg = ContactMessage(
            name=user.name,
            email=user.email or 'noreply@sugarblush.local',
            phone=user.phone1 or '',
            message=warning_msg,
            status=warning_status
        )
        db.session.add(msg)
        db.session.commit()

    return due, due_threshold, warning_msg if warning_status == 'Warning Sent' else None


def get_or_create_guest_user():
    guest_email = 'guest@sugarblush.local'
    guest = User.query.filter_by(email=guest_email).first()
    if not guest:
        guest = User(name='Guest Customer', email=guest_email, role='guest')
        guest.set_password('guest_default_password')
        db.session.add(guest)
        db.session.commit()
    return guest


def ensure_order_number_column():
    with app.app_context():
        try:
            result = db.session.execute(text("PRAGMA table_info('order')")).fetchall()
            cols = [row[1] for row in result]
            if 'order_number' not in cols:
                db.session.execute(text("ALTER TABLE `order` ADD COLUMN order_number VARCHAR(64)"))
            if 'order_name' not in cols:
                db.session.execute(text("ALTER TABLE `order` ADD COLUMN order_name VARCHAR(150)"))
            if 'payment_method' not in cols:
                db.session.execute(text("ALTER TABLE `order` ADD COLUMN payment_method VARCHAR(50)"))
            if 'payment_details' not in cols:
                db.session.execute(text("ALTER TABLE `order` ADD COLUMN payment_details TEXT"))
            if 'payment_proof' not in cols:
                db.session.execute(text("ALTER TABLE `order` ADD COLUMN payment_proof VARCHAR(255)"))
            if 'amount_paid' not in cols:
                db.session.execute(text("ALTER TABLE `order` ADD COLUMN amount_paid FLOAT DEFAULT 0.0"))
            db.session.commit()
        except Exception:
            db.session.rollback()


def ensure_product_columns():
    with app.app_context():
        try:
            result = db.session.execute(text("PRAGMA table_info('product')")).fetchall()
            cols = [row[1] for row in result]
            if 'price' not in cols:
                db.session.execute(text("ALTER TABLE product ADD COLUMN price FLOAT DEFAULT 0.0"))
            if 'sale_price' not in cols:
                db.session.execute(text("ALTER TABLE product ADD COLUMN sale_price FLOAT DEFAULT 0.0"))
            if 'making_cost' not in cols:
                db.session.execute(text("ALTER TABLE product ADD COLUMN making_cost FLOAT DEFAULT 0.0"))
            if 'discount' not in cols:
                db.session.execute(text("ALTER TABLE product ADD COLUMN discount FLOAT DEFAULT 0.0"))
            if 'stock' not in cols:
                db.session.execute(text("ALTER TABLE product ADD COLUMN stock INTEGER DEFAULT 0"))
            if 'is_active' not in cols:
                db.session.execute(text("ALTER TABLE product ADD COLUMN is_active BOOLEAN DEFAULT 1"))
            # synchronize existing values so both columns match
            if 'price' in cols and 'sale_price' in cols:
                db.session.execute(text("UPDATE product SET sale_price=price WHERE sale_price IS NULL OR sale_price=0.0"))
                db.session.execute(text("UPDATE product SET price=sale_price WHERE price IS NULL OR price=0.0"))
            db.session.commit()
        except Exception:
            db.session.rollback()


def ensure_searchquery_column():
    with app.app_context():
        try:
            result = db.session.execute(text("PRAGMA table_info('search_query')")).fetchall()
            cols = [row[1] for row in result]
            if 'term' not in cols:
                db.session.execute(text("ALTER TABLE search_query ADD COLUMN term VARCHAR(255)"))
            if 'count' not in cols:
                db.session.execute(text("ALTER TABLE search_query ADD COLUMN count INTEGER DEFAULT 1"))
            if 'last_searched' not in cols:
                db.session.execute(text("ALTER TABLE search_query ADD COLUMN last_searched DATETIME"))
            db.session.commit()
        except Exception:
            db.session.rollback()


def ensure_orderitem_columns():
    with app.app_context():
        try:
            result = db.session.execute(text("PRAGMA table_info('order_item')")).fetchall()
            cols = [row[1] for row in result]
            if 'making_cost' not in cols:
                db.session.execute(text("ALTER TABLE order_item ADD COLUMN making_cost FLOAT DEFAULT 0.0"))
            db.session.commit()
        except Exception:
            db.session.rollback()


def ensure_custom_order_number_column():
    with app.app_context():
        try:
            result = db.session.execute(text("PRAGMA table_info('custom_order')")).fetchall()
            cols = [row[1] for row in result]
            if 'custom_order_number' not in cols:
                db.session.execute(text("ALTER TABLE custom_order ADD COLUMN custom_order_number VARCHAR(64)"))
            if 'customer_name' not in cols:
                db.session.execute(text("ALTER TABLE custom_order ADD COLUMN customer_name VARCHAR(120)"))
            if 'customer_email' not in cols:
                db.session.execute(text("ALTER TABLE custom_order ADD COLUMN customer_email VARCHAR(120)"))
            db.session.commit()
        except Exception:
            db.session.rollback()

def init_db():
    with app.app_context():
        db.create_all()
        ensure_order_number_column()
        ensure_product_columns()
        ensure_orderitem_columns()
        ensure_custom_order_number_column()
        ensure_searchquery_column()
        if not User.query.filter_by(email='admin@bakery.com').first():
            admin = User(name='Admin', email='admin@bakery.com', role='admin')
            admin.set_password('admin123')
            db.session.add(admin)
            db.session.commit()

            # Seed categories and products
            c1 = Category(name='Cakes')
            c2 = Category(name='Cookies')
            c3 = Category(name='Bread')
            db.session.add_all([c1, c2, c3])
            db.session.commit()

            p1 = Product(name='Chocolate Fudge Cake', description='Rich chocolate cake', sale_price=3200, making_cost=1500, discount=15, category=c1, image='https://via.placeholder.com/400x300?text=Cake', stock=20)
            p2 = Product(name='Blueberry Muffin', description='Fresh muffins', sale_price=180, making_cost=80, discount=10, category=c2, image='https://via.placeholder.com/400x300?text=Muffin', stock=100)
            p3 = Product(name='Garlic Bread', description='Crunchy garlic bread', sale_price=220, making_cost=90, discount=0, category=c3, image='https://via.placeholder.com/400x300?text=Bread', stock=50)
            db.session.add_all([p1, p2, p3])
            db.session.commit()

            a1 = Announcement(title='Grand Opening Deal', content='Buy one get one free on all cookies!')
            a2 = Announcement(title='Ramadan Special', content='Free delivery on orders of 5000 PKR and above.')
            db.session.add_all([a1, a2])
            db.session.commit()

            b1 = PromotionBanner(image='https://via.placeholder.com/1200x400?text=Promo+1', active=True)
            b2 = PromotionBanner(image='https://via.placeholder.com/1200x400?text=Promo+2', active=True)
            db.session.add_all([b1, b2])
            db.session.commit()

@app.context_processor
def inject_globals():
    categories = Category.query.order_by(Category.name).all()
    promotions = PromotionBanner.query.filter_by(active=True).all()
    announcements = Announcement.query.order_by(Announcement.created_at.desc()).limit(3).all()
    cart_items = session.get('cart', {})
    total_items = sum(item['quantity'] for item in cart_items.values()) if cart_items else 0
    logo_setting = None

    settings = {}
    try:
        from models import SiteSetting
        for key in ['site_logo', 'bank_details', 'due_warning_threshold', 'stock_warning_threshold', 'social_facebook', 'social_instagram', 'social_youtube', 'social_tiktok', 'about_image', 'about_text']:
            setting_obj = SiteSetting.query.filter_by(key=key).first()
            settings[key] = setting_obj.value if setting_obj else ''
        if settings.get('site_logo'):
            logo_setting = settings['site_logo']
    except Exception:
        settings = {k: '' for k in ['site_logo', 'bank_details', 'due_warning_threshold', 'stock_warning_threshold', 'social_facebook', 'social_instagram', 'social_youtube', 'social_tiktok', 'about_image', 'about_text']}

    return dict(
        categories=categories,
        promotions=promotions,
        announcements=announcements,
        cart_count=total_items,
        datetime=datetime,
        site_logo=logo_setting,
        facebook_url=settings.get('social_facebook', '') or 'https://www.facebook.com/share/1CNgGnYDxp/',
        instagram_url=settings.get('social_instagram', '') or 'https://www.instagram.com/sugarblush_bakers?igsh=MWp0Ymk4eWx1NGV1eg==',
        youtube_url=settings.get('social_youtube', '') or 'https://youtube.com/@sugarblush_bakers?si=tzmkz5mJLPuXHEif',
        tiktok_url=settings.get('social_tiktok', '') or 'https://www.tiktok.com/@sugarblush_vibes?_r=1&_t=ZN-94gFTOBCsPW',
        about_image=settings.get('about_image', '') or url_for('static', filename='uploads/SUGAR SLUSH (1).png'),
        about_text=settings.get('about_text', '')
    )

@app.route('/')
def home():
    products = Product.query.order_by(Product.created_at.desc()).limit(4).all()
    featured = Product.query.order_by(Product.discount.desc()).limit(4).all()

    due_amount = 0.0
    due_threshold = 0.0
    due_status = ''
    warning_note = None

    if current_user.is_authenticated:
        due_amount = get_user_complete_due(current_user.id)

        setting = SiteSetting.query.filter_by(key='due_warning_threshold').first()
        due_threshold = float(setting.value) if setting and setting.value else 0.0
        if due_threshold > 0 and due_amount >= due_threshold:
            due_status = f"Warning: Amount due exceeds threshold ({due_threshold})"

        if due_amount > 0 and due_threshold > 0 and due_amount >= due_threshold:
            warning_record = ContactMessage.query.filter_by(email=current_user.email, status='Warning Sent').order_by(ContactMessage.created_at.desc()).first()
            if warning_record:
                warning_note = warning_record.message

        if due_amount <= 0:
            completed_order = Order.query.filter_by(user_id=current_user.id, status='Complete').order_by(Order.created_at.desc()).first()
            if completed_order:
                warning_note = None
                completion_note = f"Order {completed_order.order_number or completed_order.id} is complete. No outstanding dues."

        if current_user.role != 'admin':
            ensure_user_due_warning(current_user)

    return render_template(
        'home.html',
        products=products,
        featured=featured,
        due_amount=due_amount,
        due_threshold=due_threshold,
        due_status=due_status,
        warning_note=warning_note
    )

@app.route('/about')
def about():
    stats = {
        'products_sold': 1250,
        'happy_customers': 1080,
        'years_experience': 6,
    }
    return render_template('about.html', stats=stats)

@app.route('/contact', methods=['GET', 'POST'])
def contact():
    message_history = []
    selected_email = None

    if current_user.is_authenticated:
        selected_email = current_user.email
    
    if request.method == 'POST':
        name = request.form.get('name')
        email = request.form.get('email')
        phone = request.form.get('phone')
        message = request.form.get('message')
        selected_email = email
        if not (name and email and phone and message):
            flash('Please fill all fields.', 'danger')
        else:
            contact_message = ContactMessage(name=name, email=email, phone=phone, message=message)
            db.session.add(contact_message)
            db.session.commit()
            flash('Message received! We will reply as soon as possible.', 'success')
            return redirect(url_for('contact'))

    if selected_email:
        message_history = ContactMessage.query.filter_by(email=selected_email).order_by(ContactMessage.created_at.desc()).all()

    return render_template('contact.html', message_history=message_history, selected_email=selected_email)


@app.route('/shop')
def shop():
    query = request.args.get('q', '')
    cat_id = request.args.get('category_id')
    cat_name = request.args.get('category', '')
    page = int(request.args.get('page', 1))
    per_page = 9

    products_q = Product.query.filter(Product.is_active == True)
    if cat_id:
        try:
            cat_id = int(cat_id)
            products_q = products_q.filter(Product.category_id == cat_id)
        except ValueError:
            cat_id = None
    elif cat_name:
        products_q = products_q.join(Category).filter(Category.name.ilike(f'%{cat_name}%'))

    if query:
        products_q = products_q.filter(Product.name.ilike(f'%{query}%') | Product.description.ilike(f'%{query}%'))

        search_term = query.strip()
        if search_term:
            existing_search = db.session.query(SearchQuery).filter(db.func.lower(SearchQuery.query_text) == search_term.lower()).first()
            if existing_search:
                existing_search.count += 1
                existing_search.last_searched = datetime.utcnow()
            else:
                existing_search = SearchQuery(query_text=search_term, count=1, last_searched=datetime.utcnow())
                db.session.add(existing_search)
            db.session.commit()

    products = products_q.order_by(Product.created_at.desc()).paginate(page=page, per_page=per_page, error_out=False)
    # ensure page stays in range
    if page > products.pages and products.pages > 0:
        return redirect(url_for('shop', page=products.pages, q=query, category=cat_name, category_id=cat_id))
    if page < 1:
        return redirect(url_for('shop', page=1, q=query, category=cat_name, category_id=cat_id))
    return render_template('shop.html', products=products, page=page, per_page=per_page)

@app.route('/product/<int:product_id>', methods=['GET', 'POST'])
def product_detail(product_id):
    product = Product.query.get_or_404(product_id)
    if request.method == 'POST':
        qty = int(request.form.get('quantity', 1))
        addon_ids = request.form.getlist('addon_ids')
        return add_to_cart_item(product_id, qty, addon_ids)
    return render_template('product_detail.html', product=product)

def add_to_cart_item(product_id, quantity=1, addon_ids=None, redirect_url=None):
    cart = session.get('cart', {})
    product = Product.query.get_or_404(product_id)
    addon_ids = addon_ids or []

    if product.stock <= 0:
        flash('Product is out of stock.', 'danger')
        return redirect(request.referrer or url_for('shop'))

    item = cart.get(str(product_id), {
        'name': product.name,
        'price': product.price_after_discount,
        'making_cost': product.making_cost,
        'quantity': 0,
        'image': product.image,
        'addons': []
    })

    item['quantity'] += quantity
    if item['quantity'] < 1:
        item['quantity'] = 1

    if item['quantity'] > product.stock:
        item['quantity'] = product.stock
        flash(f'Quantity adjusted to available stock ({product.stock}).', 'warning')

    # Keep existing addons and add new ones if selected
    existing_addon_ids = {a['id'] for a in item.get('addons', [])}
    for addon_id in addon_ids:
        if addon_id and int(addon_id) not in existing_addon_ids:
            p_addon = ProductAddOn.query.get(int(addon_id))
            if p_addon:
                item.setdefault('addons', []).append({
                    'id': p_addon.id,
                    'name': p_addon.name,
                    'price': p_addon.price,
                    'image': p_addon.image
                })

    cart[str(product_id)] = item
    session['cart'] = cart
    flash(f'{product.name} added to cart', 'success')
    if redirect_url:
        return redirect(redirect_url)
    return redirect(request.referrer or url_for('shop'))

@app.route('/add_to_cart/<int:product_id>', methods=['POST'])
def add_to_cart(product_id):
    if not current_user.is_authenticated:
        flash('Please signup or login to add items to cart.', 'info')
        return redirect(url_for('signup'))
    qty = int(request.form.get('quantity', 1))
    addon_ids = request.form.getlist('addon_ids')
    return add_to_cart_item(product_id, qty, addon_ids)

@app.route('/buy_now/<int:product_id>', methods=['POST'])
def buy_now(product_id):
    if not current_user.is_authenticated:
        flash('Please signup or login to buy items.', 'info')
        return redirect(url_for('signup'))
    qty = int(request.form.get('quantity', 1))
    addon_ids = request.form.getlist('addon_ids')
    return add_to_cart_item(product_id, qty, addon_ids, redirect_url=url_for('checkout'))

@app.route('/cart')
@login_required
def cart():
    cart_items = session.get('cart', {})
    total_price = 0
    for v in cart_items.values():
        addon_sum = sum(addon['price'] for addon in v.get('addons', []))
        total_price += (v['price'] + addon_sum) * v['quantity']
    return render_template('cart.html', cart_items=cart_items, total_price=total_price)

@app.route('/update_cart', methods=['POST'])
def update_cart():
    cart = session.get('cart', {})
    for product_id, data in request.form.items():
        if product_id.startswith('qty_'):
            pid = product_id.replace('qty_', '')
            qty = int(data)
            if pid in cart:
                if qty <= 0:
                    cart.pop(pid)
                else:
                    cart[pid]['quantity'] = qty
    session['cart'] = cart
    flash('Cart updated successfully.', 'success')
    return redirect(url_for('cart'))

@app.route('/remove_from_cart/<int:product_id>')
def remove_from_cart(product_id):
    cart = session.get('cart', {})
    cart.pop(str(product_id), None)
    session['cart'] = cart
    flash('Item removed from cart', 'warning')
    return redirect(url_for('cart'))

@app.route('/checkout', methods=['GET', 'POST'])
def checkout():
    cart_items = session.get('cart', {})
    if not cart_items:
        flash('Your cart is empty.', 'danger')
        return redirect(url_for('shop'))

    is_guest = not current_user.is_authenticated

    if request.method == 'GET':
        total_price = 0
        for v in cart_items.values():
            addon_sum = sum(addon['price'] for addon in v.get('addons', []))
            total_price += (v['price'] + addon_sum) * v['quantity']
        return render_template('checkout.html', cart_items=cart_items, total_price=total_price, is_guest=is_guest)

    # POST logic (place order)
    if is_guest:
        guest_name = request.form.get('guest_name')
        guest_email = request.form.get('guest_email')
        if not guest_name or not guest_email:
            flash('Please provide name and email to complete checkout.', 'danger')
            return redirect(url_for('checkout'))
        user = get_or_create_guest_user()
    else:
        user = current_user

    total = 0

    for v in cart_items.values():
        addon_sum = sum(addon['price'] for addon in v.get('addons', []))
        total += (v['price'] + addon_sum) * v['quantity']

    if len(cart_items) == 1:
        first_item = next(iter(cart_items.values()))
        order_name = first_item.get('name', 'Order')
    else:
        order_name = 'Multiple items'

    # Validate stock before placing order
    for pid, item in cart_items.items():
        product = Product.query.get(int(pid))
        if not product or product.stock <= 0:
            flash(f"{item.get('name', 'Product')} is out of stock.", 'danger')
            return redirect(url_for('cart'))
        if item['quantity'] > product.stock:
            flash(f"Insufficient stock for {product.name}. Available: {product.stock}", 'danger')
            return redirect(url_for('cart'))

    if is_guest:
        order_name = request.form.get('guest_name', order_name)

    payment_method = request.form.get('payment_method', 'Cash on Delivery')
    payment_details = ''
    if payment_method == 'Online':
        bank_setting = SiteSetting.query.filter_by(key='bank_details').first()
        payment_details = bank_setting.value if bank_setting else 'Bank details not configured.'

    status = 'Pending Payment' if payment_method == 'Online' else 'Under Process'

    order = Order(
        user_id=user.id,
        total_price=total,
        order_number=generate_order_number(),
        order_name=order_name,
        payment_method=payment_method,
        payment_details=payment_details,
        status=status
    )
    db.session.add(order)
    db.session.flush()
    for pid, item in cart_items.items():
        oi = OrderItem(order_id=order.id, product_id=int(pid), quantity=item['quantity'], price=item['price'], making_cost=item.get('making_cost', 0.0))
        db.session.add(oi)
        product = Product.query.get(int(pid))
        if product:
            product.stock = max(0, product.stock - item['quantity'])
            db.session.add(product)
        for addon in item.get('addons', []):
            order_addon = AddOn(order_id=order.id, name=addon['name'], price=addon['price'])
            db.session.add(order_addon)
    db.session.commit()
    session['cart'] = {}
    if payment_method == 'Online':
        return redirect(url_for('online_payment_instructions', order_id=order.id))

    flash('Checkout successful. Your order has been placed.', 'success')
    return redirect(url_for('order_history'))

@app.route('/online-payment/<int:order_id>', methods=['GET', 'POST'])
def online_payment_instructions(order_id):
    order = Order.query.get_or_404(order_id)
    if order.user_id is not None:
        if not current_user.is_authenticated or order.user_id != current_user.id:
            abort(403)

    if request.method == 'POST':
        file = request.files.get('payment_proof')
        if file and file.filename:
            filename = secure_filename(file.filename)
            proof_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(proof_path)
            order.payment_proof = url_for('static', filename=f'uploads/{filename}')

        order.status = 'Under Process'
        db.session.commit()
        flash('Payment proof uploaded and order is now under process.', 'success')
        return redirect(url_for('order_history'))

    return render_template('online_payment.html', order=order)

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        data = request.form
        if User.query.filter_by(email=data.get('email')).first():
            flash('Email already exists.', 'danger')
            return redirect(url_for('signup'))
        user = User(
            name=data.get('name'),
            email=data.get('email'),
            phone1=data.get('phone1'),
            phone2=data.get('phone2'),
            country=data.get('country'),
            state=data.get('state'),
            city=data.get('city'),
            postal_code=data.get('postal_code'),
            address1=data.get('address1'),
            address2=data.get('address2'),
            address3=data.get('address3'),
        )
        user.set_password(data.get('password'))
        db.session.add(user)
        db.session.commit()
        flash('Signup successful! Please login.', 'success')
        return redirect(url_for('login'))
    return render_template('auth/signup.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = User.query.filter_by(email=request.form['email']).first()
        if user and user.check_password(request.form['password']):
            if user.role == 'suspended':
                flash('Account suspended. Contact admin to reactivate.', 'danger')
                return redirect(url_for('login'))
            login_user(user)
            flash('Login successful.', 'success')
            next_page = request.args.get('next') or url_for('home')
            return redirect(next_page)
        flash('Invalid credentials.', 'danger')
    return render_template('auth/login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Logged out.', 'info')
    return redirect(url_for('home'))

@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    if request.method == 'POST':
        user = current_user
        user.name = request.form.get('name')
        user.phone1 = request.form.get('phone1')
        user.phone2 = request.form.get('phone2')
        user.country = request.form.get('country')
        user.state = request.form.get('state')
        user.city = request.form.get('city')
        user.postal_code = request.form.get('postal_code')
        user.address1 = request.form.get('address1')
        user.address2 = request.form.get('address2')
        user.address3 = request.form.get('address3')
        if request.form.get('password'):
            current_user.set_password(request.form.get('password'))
        db.session.commit()
        flash('Profile updated.', 'success')
        return redirect(url_for('profile'))

    due_amount = get_user_complete_due(current_user.id)

    due_threshold_setting = SiteSetting.query.filter_by(key='due_warning_threshold').first()
    due_threshold = float(due_threshold_setting.value) if due_threshold_setting and due_threshold_setting.value else 0.0
    due_status = ''
    if due_threshold > 0 and due_amount >= due_threshold:
        due_status = f"Warning: Amount due exceeds threshold ({due_threshold})"

    warning_note = None
    if due_amount > 0 and due_threshold > 0 and due_amount >= due_threshold:
        warning_record = ContactMessage.query.filter_by(email=current_user.email, status='Warning Sent').order_by(ContactMessage.created_at.desc()).first()
        if warning_record:
            warning_note = warning_record.message

    completion_note = None
    if due_amount <= 0:
        completed_order = Order.query.filter_by(user_id=current_user.id, status='Complete').order_by(Order.created_at.desc()).first()
        if completed_order:
            completion_note = f"Congrats! Order {completed_order.order_number or completed_order.id} is complete."

    if current_user.role != 'admin':
        ensure_user_due_warning(current_user)

    return render_template(
        'profile.html',
        due_amount=due_amount,
        due_threshold=due_threshold,
        due_status=due_status,
        warning_note=warning_note,
        completion_note=completion_note
    )

@app.route('/order_history')
@login_required
def order_history():
    orders = Order.query.filter_by(user_id=current_user.id).order_by(Order.created_at.desc()).all()
    return render_template('orders.html', orders=orders)

@app.route('/order/<int:order_id>/invoice')
@login_required
def invoice(order_id):
    if current_user.role == 'admin':
        order = Order.query.filter_by(id=order_id).first_or_404()
    else:
        order = Order.query.filter_by(id=order_id, user_id=current_user.id).first_or_404()

    total_making_cost = sum(item.making_cost * item.quantity for item in order.items)
    total_profit = sum(item.profit for item in order.items)
    return render_template('invoice.html', order=order, total_making_cost=total_making_cost, total_profit=total_profit)

@app.route('/custom_orders', methods=['GET', 'POST'])
@login_required
def custom_orders():
    if request.method == 'POST':
        title = request.form.get('title')
        description = request.form.get('description')
        quantity = int(request.form.get('quantity', 1))
        customer_name = request.form.get('customer_name', current_user.name)
        customer_email = request.form.get('customer_email', current_user.email)

        if not title or not description or not customer_name or not customer_email:
            flash('Please fill all required fields including customer name and email.', 'danger')
            return redirect(url_for('custom_orders'))

        image_file = request.files.get('image')
        image_name = None
        if image_file and image_file.filename:
            filename = secure_filename(image_file.filename)
            image_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            image_file.save(image_path)
            image_name = url_for('static', filename=f'uploads/{filename}')

        custom = CustomOrder(
            custom_order_number=f"CO{generate_order_number()}",
            user_id=current_user.id,
            customer_name=customer_name,
            customer_email=customer_email,
            title=title,
            description=description,
            quantity=quantity,
            uploaded_image=image_name
        )
        db.session.add(custom)
        db.session.commit()
        flash('Custom request submitted.', 'success')
        return redirect(url_for('custom_orders'))

    requests = CustomOrder.query.filter_by(user_id=current_user.id).order_by(CustomOrder.created_at.desc()).all()
    return render_template('custom_orders.html', requests=requests)

@app.route('/custom_orders/confirm/<int:custom_id>')
@login_required
def confirm_custom(custom_id):
    custom = CustomOrder.query.filter_by(id=custom_id, user_id=current_user.id).first_or_404()
    if custom.estimate_price is None:
        flash('Estimate not ready yet.', 'warning')
        return redirect(url_for('custom_orders'))
    if custom.status != 'Quoted':
        flash('Order not in confirmable state.', 'warning')
        return redirect(url_for('custom_orders'))
    custom.status = 'Confirmed'
    db.session.commit()
    flash('Custom order confirmed.', 'success')
    return redirect(url_for('custom_orders'))

@app.route('/custom_orders/<int:custom_id>/update_quantity', methods=['POST'])
@login_required
def update_custom_quantity(custom_id):
    custom = CustomOrder.query.filter_by(id=custom_id, user_id=current_user.id).first_or_404()
    if custom.status not in ['Quoted', 'Confirmed']:
        flash('Cannot change quantity for this custom order at this stage.', 'warning')
        return redirect(url_for('custom_orders'))

    try:
        quantity = int(request.form.get('quantity', custom.quantity))
    except ValueError:
        flash('Invalid quantity value.', 'danger')
        return redirect(url_for('custom_orders'))

    if quantity < 1:
        flash('Quantity must be at least 1.', 'danger')
        return redirect(url_for('custom_orders'))

    custom.quantity = quantity
    db.session.commit()
    flash('Custom order quantity updated.', 'success')
    return redirect(url_for('custom_orders'))

@app.route('/custom_orders/checkout/<int:custom_id>', methods=['GET', 'POST'])
@login_required
def custom_order_checkout(custom_id):
    custom = CustomOrder.query.filter_by(id=custom_id, user_id=current_user.id).first_or_404()
    if custom.estimate_price is None or custom.status not in ['Quoted', 'Confirmed']:
        flash('This custom order is not ready for checkout.', 'warning')
        return redirect(url_for('custom_orders'))

    total_price = custom.estimate_price * custom.quantity

    if request.method == 'POST':
        payment_method = request.form.get('payment_method', 'Cash on Delivery')
        payment_details = ''

        # user checkout triggers order creation in pending approval state
        status = 'Pending Approval'
        if payment_method == 'Online':
            status = 'Pending Payment'
            bank_setting = SiteSetting.query.filter_by(key='bank_details').first()
            payment_details = bank_setting.value if bank_setting else 'Bank details not configured.'

        order = Order(
            user_id=current_user.id,
            total_price=total_price,
            order_number=generate_order_number(),
            order_name=custom.title,
            payment_method=payment_method,
            payment_details=payment_details,
            status=status
        )
        db.session.add(order)
        db.session.flush()

        order_item = OrderItem(order_id=order.id, product_id=0, quantity=custom.quantity, price=custom.estimate_price)
        db.session.add(order_item)

        custom.status = 'Converted'
        db.session.commit()

        if payment_method == 'Online':
            return redirect(url_for('online_payment_instructions', order_id=order.id))

        flash('Order placed and is pending approval.', 'success')
        return redirect(url_for('order_history'))

    return render_template('custom_checkout.html', custom=custom, total_price=total_price)


# NOTE: The custom order conversion to actual Order is handled by admin in admin_custom_orders route.

def parse_date_range(start_date, end_date):
    try:
        start = datetime.strptime(start_date, '%Y-%m-%d') if start_date else None
    except Exception:
        start = None
    try:
        end = datetime.strptime(end_date, '%Y-%m-%d') if end_date else None
    except Exception:
        end = None
    if start and not end:
        end = datetime.utcnow()
    return start, end


def get_date_range(filter_type, start_date='', end_date=''):
    start, end = parse_date_range(start_date, end_date)
    now = datetime.utcnow()

    if filter_type == 'daily':
        end = now
        start = now - timedelta(days=30)
    elif filter_type == 'weekly':
        end = now
        start = now - timedelta(days=7)
    elif filter_type == 'monthly':
        end = now
        start = now - timedelta(days=365)
    elif filter_type == 'yearly':
        end = now
        start = now - timedelta(days=365 * 5)
    elif filter_type == 'custom':
        if not start or not end:
            end = now
            start = now - timedelta(days=30)
    else:
        end = now
        start = now - timedelta(days=30)

    if start and end:
        end = datetime(end.year, end.month, end.day, 23, 59, 59)

    return start, end


def generate_chart_image(chart_type, start, end):
    fig = Figure(figsize=(10, 4), tight_layout=True)
    ax = fig.add_subplot(111)

    if chart_type in ['overall_sales', 'sales_trend']:
        rows = db.session.query(
            db.func.strftime('%Y-%m-%d', Order.created_at).label('period'),
            db.func.coalesce(db.func.sum(Order.total_price), 0.0).label('value')
        ).filter(Order.created_at >= start, Order.created_at <= end).group_by('period').order_by('period').all()

        x = [r.period for r in rows]
        y = [float(r.value) for r in rows]
        ax.plot(x, y, marker='o', color='#0d6efd')
        ax.set_title('Overall Sales Overview')
        ax.set_ylabel('Total Sales (PKR)')

    elif chart_type == 'profit_trend':
        rows = db.session.query(
            db.func.strftime('%Y-%m-%d', Order.created_at).label('period'),
            db.func.coalesce(db.func.sum((OrderItem.price - OrderItem.making_cost) * OrderItem.quantity), 0.0).label('value')
        ).join(OrderItem, OrderItem.order_id == Order.id).filter(Order.created_at >= start, Order.created_at <= end).group_by('period').order_by('period').all()

        x = [r.period for r in rows]
        y = [float(r.value) for r in rows]
        ax.plot(x, y, marker='o', color='#198754')
        ax.set_title('Profit Trend')
        ax.set_ylabel('Total Profit (PKR)')

    elif chart_type == 'orders_trend':
        rows = db.session.query(
            db.func.strftime('%Y-%m-%d', Order.created_at).label('period'),
            db.func.count(Order.id).label('value')
        ).filter(Order.created_at >= start, Order.created_at <= end).group_by('period').order_by('period').all()

        x = [r.period for r in rows]
        y = [r.value for r in rows]
        ax.plot(x, y, marker='o', color='#ffc107')
        ax.set_title('Orders Trend')
        ax.set_ylabel('Number of Orders')

    elif chart_type == 'top_products':
        rows = db.session.query(
            Product.name,
            db.func.coalesce(db.func.sum(OrderItem.quantity), 0).label('quantity')
        ).join(OrderItem, OrderItem.product_id == Product.id).join(Order, OrderItem.order_id == Order.id)
        rows = rows.filter(Order.created_at >= start, Order.created_at <= end).group_by(Product.id).order_by(db.desc('quantity')).limit(10).all()

        names = [r.name for r in rows]
        qty = [r.quantity for r in rows]
        ax.barh(names[::-1], qty[::-1], color='#6f42c1')
        ax.set_title('Top Selling Products')
        ax.set_xlabel('Quantity Sold')

    elif chart_type == 'category_sales':
        rows = db.session.query(
            Category.name,
            db.func.coalesce(db.func.sum(OrderItem.quantity * OrderItem.price), 0.0).label('value')
        ).join(Product, Product.category_id == Category.id).join(OrderItem, OrderItem.product_id == Product.id).join(Order, Order.id == OrderItem.order_id)
        rows = rows.filter(Order.created_at >= start, Order.created_at <= end).group_by(Category.id).all()

        labels = [r[0] for r in rows]
        sizes = [float(r[1]) for r in rows]
        ax.pie(sizes or [1], labels=labels, autopct='%1.1f%%', startangle=140, textprops={'fontsize': 8})
        ax.set_title('Category Sales Distribution')

    elif chart_type == 'monthly_sales':
        rows = db.session.query(
            db.func.strftime('%Y-%m', Order.created_at).label('period'),
            db.func.coalesce(db.func.sum(Order.total_price), 0.0).label('value')
        ).filter(Order.created_at >= start, Order.created_at <= end).group_by('period').order_by('period').all()

        x = [r.period for r in rows]
        y = [float(r.value) for r in rows]
        ax.bar(x, y, color='#ff8c00')
        ax.set_title('Monthly Sales')
        ax.set_xlabel('Month')
        ax.set_ylabel('Sales (PKR)')
        ax.tick_params(axis='x', rotation=45)

    elif chart_type == 'monthly_profit':
        rows = db.session.query(
            db.func.strftime('%Y-%m', Order.created_at).label('period'),
            db.func.coalesce(db.func.sum((OrderItem.price - OrderItem.making_cost) * OrderItem.quantity), 0.0).label('value')
        ).join(OrderItem, OrderItem.order_id == Order.id).filter(Order.created_at >= start, Order.created_at <= end).group_by('period').order_by('period').all()

        x = [r.period for r in rows]
        y = [float(r.value) for r in rows]
        ax.bar(x, y, color='#0d6efd')
        ax.set_title('Monthly Profit')
        ax.set_xlabel('Month')
        ax.set_ylabel('Profit (PKR)')
        ax.tick_params(axis='x', rotation=45)

    elif chart_type == 'category_profit':
        rows = db.session.query(
            Category.name,
            db.func.coalesce(db.func.sum((OrderItem.price - OrderItem.making_cost) * OrderItem.quantity), 0.0).label('value')
        ).join(Product, Product.category_id == Category.id).join(OrderItem, OrderItem.product_id == Product.id).join(Order, Order.id == OrderItem.order_id)
        rows = rows.filter(Order.created_at >= start, Order.created_at <= end).group_by(Category.id).all()

        labels = [r[0] for r in rows]
        sizes = [float(r[1]) for r in rows]
        ax.pie(sizes or [1], labels=labels, autopct='%1.1f%%', startangle=140, textprops={'fontsize': 8})
        ax.set_title('Category Profit Distribution')

    elif chart_type == 'order_status':
        rows = db.session.query(Order.status, db.func.count(Order.id)).filter(Order.created_at >= start, Order.created_at <= end).group_by(Order.status).all()
        labels = [r[0] for r in rows]
        counts = [r[1] for r in rows]
        if not labels:
            labels = ['Under Process', 'Shipped', 'Complete']
            counts = [0, 0, 0]
        ax.pie(counts, labels=labels, autopct='%1.1f%%', startangle=140, textprops={'fontsize': 8})
        ax.set_title('Order Status Distribution')

    elif chart_type == 'profit_per_product':
        rows = db.session.query(
            Product.name,
            db.func.coalesce(db.func.sum((OrderItem.price - OrderItem.making_cost) * OrderItem.quantity), 0.0).label('value')
        ).join(OrderItem, Product.id == OrderItem.product_id).join(Order, Order.id == OrderItem.order_id)
        rows = rows.filter(Order.created_at >= start, Order.created_at <= end).group_by(Product.id).order_by(db.desc('value')).limit(10).all()

        names = [r[0] for r in rows]
        profits = [float(r[1]) for r in rows]
        ax.barh(names[::-1], profits[::-1], color='#198754')
        ax.set_title('Top Profit Products')
        ax.set_xlabel('Profit (PKR)')


    else:
        ax.text(0.5, 0.5, 'No chart available', ha='center')

    if chart_type in ['overall_sales', 'sales_trend', 'profit_trend', 'orders_trend']:
        ax.set_xlabel('Date')
        ax.tick_params(axis='x', rotation=45)

    buf = io.BytesIO()
    fig.savefig(buf, format='png')
    buf.seek(0)
    return buf


@app.route('/admin/dashboard')
@login_required
@admin_required
def admin_dashboard():
    total_customers = User.query.filter(User.role != 'admin').count()
    total_products = Product.query.count()
    total_orders = Order.query.count()

    start, end = get_date_range('daily')

    total_sales = db.session.query(db.func.coalesce(db.func.sum(Order.total_price), 0.0)).filter(Order.created_at >= start, Order.created_at <= end).scalar() or 0.0
    total_making_cost = db.session.query(db.func.coalesce(db.func.sum(OrderItem.making_cost * OrderItem.quantity), 0.0)).join(Order, Order.id == OrderItem.order_id).filter(Order.created_at >= start, Order.created_at <= end).scalar() or 0.0
    total_profit = total_sales - total_making_cost

    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = datetime.utcnow().replace(hour=23, minute=59, second=59, microsecond=999999)

    today_sales = db.session.query(db.func.coalesce(db.func.sum(Order.total_price), 0.0)).filter(Order.created_at >= today_start, Order.created_at <= today_end).scalar() or 0.0
    today_making_cost = db.session.query(db.func.coalesce(db.func.sum(OrderItem.making_cost * OrderItem.quantity), 0.0)).join(Order, Order.id == OrderItem.order_id).filter(Order.created_at >= today_start, Order.created_at <= today_end).scalar() or 0.0
    today_profit = today_sales - today_making_cost

    pending_orders = Order.query.filter(Order.status.in_(['Pending', 'Pending Payment', 'Under Process'])).count()
    complete_orders = Order.query.filter_by(status='Complete').count()

    pending_custom_orders = CustomOrder.query.filter(CustomOrder.status.in_(['Pending', 'Quoted', 'Confirmed', 'Under Process'])).count()

    stock_warning_setting = SiteSetting.query.filter_by(key='stock_warning_threshold').first()
    stock_warning_threshold = int(stock_warning_setting.value) if stock_warning_setting and stock_warning_setting.value and stock_warning_setting.value.isdigit() else 5
    total_stock = db.session.query(db.func.coalesce(db.func.sum(Product.stock), 0)).scalar() or 0
    low_stock_items = Product.query.filter(Product.stock <= stock_warning_threshold).count()

    dues_remaining = db.session.query(db.func.coalesce(db.func.sum(Order.total_price - Order.amount_paid), 0.0)).filter(Order.total_price > Order.amount_paid).scalar() or 0.0

    return render_template('admin/dashboard.html',
        total_sales=total_sales,
        total_profit=total_profit,
        total_orders=total_orders,
        total_customers=total_customers,
        total_products=total_products,
        total_stock=total_stock,
        low_stock_items=low_stock_items,
        stock_warning_threshold=stock_warning_threshold,
        sales_overall_chart=url_for('admin_chart', chart_type='overall_sales', filter='daily'),
        today_sales=today_sales,
        today_profit=today_profit,
        pending_orders=pending_orders,
        pending_custom_orders=pending_custom_orders,
        complete_orders=complete_orders,
        dues_remaining=dues_remaining
    )

@app.route('/admin/stock', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_stock():
    if request.method == 'POST':
        prod_id = int(request.form.get('product_id', 0))
        stock_val = max(0, int(request.form.get('stock', 0)))
        product = Product.query.get_or_404(prod_id)
        product.stock = stock_val
        db.session.commit()
        flash(f'Stock updated for {product.name}.', 'success')
        return redirect(url_for('admin_stock'))

    stock_warning_setting = SiteSetting.query.filter_by(key='stock_warning_threshold').first()
    stock_warning_threshold = int(stock_warning_setting.value) if stock_warning_setting and stock_warning_setting.value and stock_warning_setting.value.isdigit() else 5

    products = Product.query.order_by(Product.name).all()
    total_stock = db.session.query(db.func.coalesce(db.func.sum(Product.stock), 0)).scalar() or 0
    low_stock_items = Product.query.filter(Product.stock <= stock_warning_threshold).count()

    category_stock = db.session.query(
        Category.name,
        db.func.coalesce(db.func.sum(Product.stock), 0).label('stock')
    ).join(Product, Product.category_id == Category.id).group_by(Category.id).all()

    low_stock_products = Product.query.filter(Product.stock <= stock_warning_threshold).order_by(Product.stock.asc()).all()

    return render_template('admin/stock.html',
        products=products,
        total_stock=total_stock,
        low_stock_items=low_stock_items,
        stock_warning_threshold=stock_warning_threshold,
        category_stock=category_stock,
        low_stock_products=low_stock_products
    )

@app.route('/admin/stock_report')
@login_required
@admin_required
def admin_stock_report():
    stock_warning_setting = SiteSetting.query.filter_by(key='stock_warning_threshold').first()
    stock_warning_threshold = int(stock_warning_setting.value) if stock_warning_setting and stock_warning_setting.value and stock_warning_setting.value.isdigit() else 5

    products = Product.query.order_by(Product.name).all()
    total_stock = db.session.query(db.func.coalesce(db.func.sum(Product.stock), 0)).scalar() or 0
    low_stock_items = Product.query.filter(Product.stock <= stock_warning_threshold).count()

    category_stock = db.session.query(
        Category.name,
        db.func.coalesce(db.func.sum(Product.stock), 0).label('stock')
    ).join(Product, Product.category_id == Category.id).group_by(Category.id).all()

    return render_template('admin/stock_report.html',
        products=products,
        total_stock=total_stock,
        low_stock_items=low_stock_items,
        stock_warning_threshold=stock_warning_threshold,
        category_stock=category_stock
    )

@app.route('/admin/top_search_report')
@login_required
@admin_required
def admin_top_search_report():
    top_searches = db.session.query(SearchQuery).order_by(SearchQuery.count.desc(), SearchQuery.last_searched.desc()).limit(50).all()
    total_searches = db.session.query(db.func.coalesce(db.func.sum(SearchQuery.count), 0)).scalar() or 0
    unique_searches = db.session.query(db.func.count(SearchQuery.id)).scalar() or 0
    average_searches_per_term = (total_searches / unique_searches) if unique_searches > 0 else 0
    top_term = top_searches[0] if top_searches else None
    top_five = top_searches[:5]
    return render_template('admin/top_search_report.html', top_searches=top_searches, total_searches=total_searches, unique_searches=unique_searches, average_searches_per_term=average_searches_per_term, top_term=top_term, top_five=top_five)

@app.route('/admin/top_search_report/pdf')
@login_required
@admin_required
def admin_top_search_report_pdf():
    if not HAS_REPORTLAB:
        flash('PDF generation is not available. Please install reportlab.', 'danger')
        return redirect(url_for('admin_top_search_report'))

    top_searches = db.session.query(SearchQuery).order_by(SearchQuery.count.desc(), SearchQuery.last_searched.desc()).limit(100).all()
    total_searches = db.session.query(db.func.coalesce(db.func.sum(SearchQuery.count), 0)).scalar() or 0
    unique_searches = db.session.query(db.func.count(SearchQuery.id)).scalar() or 0
    average_searches_per_term = (total_searches / unique_searches) if unique_searches > 0 else 0
    top_term = top_searches[0] if top_searches else None
    top_five = top_searches[:5]

    buf = io.BytesIO()
    p = canvas.Canvas(buf, pagesize=letter)
    width, height = letter
    margin = 40

    p.setTitle('Top Search Terms Report')
    p.setFont('Helvetica-Bold', 18)
    p.setFillColor(colors.HexColor('#1a237e'))
    p.drawString(margin, height - margin, 'Sugar Blush Bakery - Top Search Terms')

    p.setFont('Helvetica', 10)
    p.setFillColor(colors.black)
    p.drawRightString(width - margin, height - margin + 4, datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC'))

    # Summary boxes
    y = height - margin - 35
    p.setFont('Helvetica-Bold', 10)
    p.drawString(margin, y, f'Total Searches: {total_searches}')
    p.drawString(margin + 220, y, f'Unique Terms: {unique_searches}')
    p.drawString(margin + 420, y, f'Avg/Term: {average_searches_per_term:.2f}')

    y -= 25
    if top_term:
        p.setFillColor(colors.HexColor('#ff6f00'))
        p.rect(margin, y - 18, width - 2*margin, 18, fill=1, stroke=0)
        p.setFillColor(colors.white)
        p.setFont('Helvetica-Bold', 11)
        p.drawString(margin + 5, y - 14, f"Top Search: {top_term.query_text} ({top_term.count} searches)")
        y -= 30
    else:
        y -= 10

    # Top 5 list
    if top_five:
        p.setFillColor(colors.HexColor('#f5f5f5'))
        p.rect(margin, y - 80, width - 2*margin, 80, fill=1, stroke=0)
        p.setFillColor(colors.black)
        p.setFont('Helvetica-Bold', 10)
        p.drawString(margin + 5, y - 5, 'Top 5 Terms')
        y -= 20
        p.setFont('Helvetica', 9)
        for idx, row in enumerate(top_five, start=1):
            value = f"{idx}. {row.query_text} ({row.count})"
            p.drawString(margin + 8, y, value)
            y -= 13
        y -= 10

    # table header
    p.setFont('Helvetica-Bold', 10)
    p.drawString(margin, y, 'Rank')
    p.drawString(margin + 60, y, 'Search Term')
    p.drawRightString(width - margin - 120, y, 'Count')
    p.drawRightString(width - margin, y, 'Last Searched')

    y -= 14
    p.setFont('Helvetica', 9)

    rank = 1
    for row in top_searches:
        if y < margin + 40:
            p.showPage()
            y = height - margin
            p.setFont('Helvetica-Bold', 10)
            p.drawString(margin, y, 'Rank')
            p.drawString(margin + 60, y, 'Search Term')
            p.drawRightString(width - margin - 120, y, 'Count')
            p.drawRightString(width - margin, y, 'Last Searched')
            y -= 14
            p.setFont('Helvetica', 9)

        display_query = (row.query_text[:38] + '...') if len(row.query_text) > 38 else row.query_text
        p.drawString(margin, y, str(rank))
        p.drawString(margin + 60, y, display_query)
        p.drawRightString(width - margin - 120, y, str(row.count))
        p.drawRightString(width - margin, y, row.last_searched.strftime('%Y-%m-%d') if row.last_searched else '-')
        y -= 12
        rank += 1

    p.showPage()
    p.save()
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name='top_search_report.pdf', mimetype='application/pdf')

@app.route('/admin/stock_report/pdf')
@login_required
@admin_required
def admin_stock_report_pdf():
    if not HAS_REPORTLAB:
        flash('PDF generation is not available. Please install reportlab.', 'danger')
        return redirect(url_for('admin_stock_report'))

    stock_warning_setting = SiteSetting.query.filter_by(key='stock_warning_threshold').first()
    stock_warning_threshold = int(stock_warning_setting.value) if stock_warning_setting and stock_warning_setting.value and stock_warning_setting.value.isdigit() else 5

    products = Product.query.order_by(Product.name).all()
    total_stock = db.session.query(db.func.coalesce(db.func.sum(Product.stock), 0)).scalar() or 0
    low_stock_items = Product.query.filter(Product.stock <= stock_warning_threshold).count()

    category_stock = db.session.query(
        Category.name,
        db.func.coalesce(db.func.sum(Product.stock), 0).label('stock')
    ).join(Product, Product.category_id == Category.id).group_by(Category.id).all()

    buf = io.BytesIO()
    p = canvas.Canvas(buf, pagesize=letter)
    width, height = letter
    margin = 40
    p.setTitle('Stock Analytics Report')

    # Header
    p.setFillColor(colors.HexColor('#333333'))
    p.setFont('Helvetica-Bold', 20)
    p.drawString(margin, height - margin, 'Sugar Blush Bakery')
    p.setFont('Helvetica', 14)
    p.drawString(margin, height - margin - 26, 'Stock Analytics Report')
    p.setFont('Helvetica', 9)
    p.drawRightString(width - margin, height - margin + 5, datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC'))

    # Summary metrics
    y = height - margin - 56
    p.setFont('Helvetica-Bold', 11)
    p.setFillColor(colors.HexColor('#223c76'))
    p.drawString(margin, y, f'Total Stock: {total_stock}')
    p.drawString(margin + 220, y, f'Low Stock (<= {stock_warning_threshold}): {low_stock_items}')
    p.drawString(margin + 430, y, f'Products: {len(products)}')

    # Category breakdown
    y -= 30
    p.setFillColor(colors.HexColor('#e0e4ea'))
    p.rect(margin, y - 18, width - 2 * margin, 18, fill=1, stroke=0)
    p.setFillColor(colors.black)
    p.setFont('Helvetica-Bold', 11)
    p.drawString(margin + 5, y - 4, 'Category')
    p.drawRightString(width - margin - 5, y - 4, 'Stock')

    y -= 8
    p.setFont('Helvetica', 10)
    if category_stock:
        for cat_name, cat_stock in category_stock:
            y -= 16
            if y < margin + 60:
                p.showPage()
                y = height - margin
            p.drawString(margin + 5, y, cat_name)
            p.drawRightString(width - margin - 5, y, str(cat_stock))
    else:
        y -= 16
        p.drawString(margin + 5, y, 'No categories found')

    # Product list
    y -= 30
    if y < margin + 160:
        p.showPage()
        y = height - margin

    p.setFillColor(colors.HexColor('#e0e4ea'))
    p.rect(margin, y - 18, width - 2 * margin, 18, fill=1, stroke=0)
    p.setFillColor(colors.black)
    p.setFont('Helvetica-Bold', 11)
    p.drawString(margin + 5, y - 4, 'Product')
    p.drawString(margin + 250, y - 4, 'Category')
    p.drawRightString(width - margin - 5, y - 4, 'Stock/Status')

    y -= 20
    p.setFont('Helvetica', 10)
    for prod in products:
        if y < margin + 60:
            p.showPage()
            y = height - margin
            p.setFillColor(colors.HexColor('#e0e4ea'))
            p.rect(margin, y - 18, width - 2 * margin, 18, fill=1, stroke=0)
            p.setFillColor(colors.black)
            p.setFont('Helvetica-Bold', 11)
            p.drawString(margin + 5, y - 4, 'Product')
            p.drawString(margin + 250, y - 4, 'Category')
            p.drawRightString(width - margin - 5, y - 4, 'Stock/Status')
            y -= 20
            p.setFont('Helvetica', 10)

        product_name = (prod.name[:35] + '...') if len(prod.name) > 35 else prod.name
        category_name = prod.category.name if prod.category else 'Uncategorized'
        status = 'OUT OF STOCK' if prod.stock <= 0 else ('LOW STOCK' if prod.stock <= stock_warning_threshold else 'IN STOCK')
        status_color = colors.red if prod.stock <= 0 else (colors.orange if prod.stock <= stock_warning_threshold else colors.green)

        p.drawString(margin + 5, y, product_name)
        p.drawString(margin + 250, y, category_name)
        p.setFillColor(status_color)
        p.drawRightString(width - margin - 5, y, f'{prod.stock} ({status})')
        p.setFillColor(colors.black)
        y -= 14

    p.showPage()
    p.save()
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name='stock_analytics_report.pdf', mimetype='application/pdf')
    y -= 14
    p.setFont('Helvetica', 10)

    for p_obj in products:
        if y < 60:
            p.showPage()
            y = 760
        product_name = (p_obj.name[:30] + '...') if len(p_obj.name) > 30 else p_obj.name
        category_name = p_obj.category.name if p_obj.category else 'Uncategorized'
        status = 'OUT' if p_obj.stock <= 0 else ('LOW' if p_obj.stock <= stock_warning_threshold else 'OK')
        p.drawString(40, y, product_name)
        p.drawString(260, y, category_name)
        p.drawString(460, y, str(p_obj.stock))
        y -= 12

    p.save()
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name='stock_analytics_report.pdf', mimetype='application/pdf')

@app.route('/admin/analytics')
@login_required
@admin_required
def admin_analytics():
    total_sales = db.session.query(db.func.coalesce(db.func.sum(Order.total_price), 0.0)).scalar() or 0.0
    total_making_cost = db.session.query(db.func.coalesce(db.func.sum(OrderItem.making_cost * OrderItem.quantity), 0.0)).scalar() or 0.0
    total_profit = total_sales - total_making_cost
    total_orders = Order.query.count()
    total_customers = User.query.filter(User.role != 'admin').count()
    total_products = Product.query.count()

    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = datetime.utcnow().replace(hour=23, minute=59, second=59, microsecond=999999)

    today_sales = db.session.query(db.func.coalesce(db.func.sum(Order.total_price), 0.0)).filter(Order.created_at >= today_start, Order.created_at <= today_end).scalar() or 0.0
    today_making_cost = db.session.query(db.func.coalesce(db.func.sum(OrderItem.making_cost * OrderItem.quantity), 0.0)).join(Order, Order.id == OrderItem.order_id).filter(Order.created_at >= today_start, Order.created_at <= today_end).scalar() or 0.0
    today_profit = today_sales - today_making_cost

    pending_orders = Order.query.filter(Order.status.in_(['Pending', 'Pending Payment', 'Under Process'])).count()
    complete_orders = Order.query.filter_by(status='Complete').count()
    pending_custom_orders = CustomOrder.query.filter(CustomOrder.status.in_(['Pending', 'Quoted', 'Confirmed', 'Under Process'])).count()

    dues_remaining = db.session.query(db.func.coalesce(db.func.sum(Order.total_price - Order.amount_paid), 0.0)).filter(Order.total_price > Order.amount_paid).scalar() or 0.0

    return render_template('admin/analytics.html',
        total_sales=total_sales,
        total_making_cost=total_making_cost,
        total_profit=total_profit,
        total_orders=total_orders,
        total_customers=total_customers,
        total_products=total_products,
        today_sales=today_sales,
        today_profit=today_profit,
        pending_orders=pending_orders,
        complete_orders=complete_orders,
        pending_custom_orders=pending_custom_orders,
        dues_remaining=dues_remaining
    )


@app.route('/admin/analytics/pdf')
@login_required
@admin_required
def admin_analytics_pdf():
    if not HAS_REPORTLAB:
        flash('PDF generation is not available. Please install reportlab.', 'danger')
        return redirect(url_for('admin_analytics'))

    total_sales = db.session.query(db.func.coalesce(db.func.sum(Order.total_price), 0.0)).scalar() or 0.0
    total_making_cost = db.session.query(db.func.coalesce(db.func.sum(OrderItem.making_cost * OrderItem.quantity), 0.0)).scalar() or 0.0
    total_profit = total_sales - total_making_cost
    total_orders = Order.query.count()
    total_customers = User.query.filter(User.role != 'admin').count()
    total_products = Product.query.count()

    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = datetime.utcnow().replace(hour=23, minute=59, second=59, microsecond=999999)

    today_sales = db.session.query(db.func.coalesce(db.func.sum(Order.total_price), 0.0)).filter(Order.created_at >= today_start, Order.created_at <= today_end).scalar() or 0.0
    today_making_cost = db.session.query(db.func.coalesce(db.func.sum(OrderItem.making_cost * OrderItem.quantity), 0.0)).join(Order, Order.id == OrderItem.order_id).filter(Order.created_at >= today_start, Order.created_at <= today_end).scalar() or 0.0
    today_profit = today_sales - today_making_cost

    pending_orders = Order.query.filter(Order.status.in_(['Pending', 'Pending Payment', 'Under Process'])).count()
    complete_orders = Order.query.filter_by(status='Complete').count()
    pending_custom_orders = CustomOrder.query.filter(CustomOrder.status.in_(['Pending', 'Quoted', 'Confirmed', 'Under Process'])).count()

    dues_remaining = db.session.query(db.func.coalesce(db.func.sum(Order.total_price - Order.amount_paid), 0.0)).filter(Order.total_price > Order.amount_paid).scalar() or 0.0

    category_distribution = db.session.query(
        Category.name,
        db.func.coalesce(db.func.sum(OrderItem.quantity * OrderItem.price), 0.0).label('sales')
    ).join(Product, Product.category_id == Category.id).join(OrderItem, OrderItem.product_id == Product.id).join(Order, Order.id == OrderItem.order_id)
    category_distribution = category_distribution.group_by(Category.id).all()

    top_products = db.session.query(
        Product.name,
        db.func.coalesce(db.func.sum(OrderItem.quantity), 0).label('qty'),
        db.func.coalesce(db.func.sum(OrderItem.quantity * OrderItem.price), 0.0).label('sales')
    ).join(OrderItem, OrderItem.product_id == Product.id).join(Order, Order.id == OrderItem.order_id)
    top_products = top_products.group_by(Product.id).order_by(db.desc('sales')).limit(10).all()

    buf = io.BytesIO()
    p = canvas.Canvas(buf, pagesize=letter)
    width, height = letter
    margin = 40

    p.setTitle('Website Analytics Report')
    p.setFont('Helvetica-Bold', 18)
    p.setFillColor(colors.HexColor('#333333'))
    p.drawString(margin, height - margin, 'Sugar Blush Bakery - Website Analytics')
    p.setFont('Helvetica', 10)
    p.drawRightString(width - margin, height - margin + 5, datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC'))

    y = height - margin - 30
    p.setFont('Helvetica-Bold', 11)
    p.setFillColor(colors.HexColor('#1752a5'))
    p.drawString(margin, y, 'Key Metrics')

    y -= 18
    p.setFont('Helvetica', 10)
    metrics = [
        ('Total Sales', f'PKR {total_sales:,.2f}'),
        ('Total Profit', f'PKR {total_profit:,.2f}'),
        ('Total Orders', str(total_orders)),
        ('Total Customers', str(total_customers)),
        ('Total Products', str(total_products)),
        ('Total Making Cost', f'PKR {total_making_cost:,.2f}'),
        ('Today Sales', f'PKR {today_sales:,.2f}'),
        ('Today Profit', f'PKR {today_profit:,.2f}'),
        ('Pending Orders', str(pending_orders)),
        ('Complete Orders', str(complete_orders)),
        ('Pending Custom Orders', str(pending_custom_orders)),
        ('Dues Remaining', f'PKR {dues_remaining:,.2f}')
    ]

    for label, value in metrics:
        if y < margin + 100:
            p.showPage(); y = height - margin
            p.setFont('Helvetica', 10)
        p.drawString(margin, y, f'{label}:')
        p.drawRightString(width - margin, y, value)
        y -= 14

    y -= 12
    p.setFont('Helvetica-Bold', 11)
    p.drawString(margin, y, 'Top Product Sales')
    y -= 16

    p.setFont('Helvetica-Bold', 10)
    p.drawString(margin, y, 'Product')
    p.drawString(margin + 260, y, 'Qty')
    p.drawRightString(width - margin, y, 'Sales')
    y -= 14

    p.setFont('Helvetica', 9)
    for prod_name, qty, sales in top_products:
        if y < margin + 80:
            p.showPage(); y = height - margin
            p.setFont('Helvetica-Bold', 10)
            p.drawString(margin, y, 'Product')
            p.drawString(margin + 260, y, 'Qty')
            p.drawRightString(width - margin, y, 'Sales')
            y -= 14
            p.setFont('Helvetica', 9)
        p.drawString(margin, y, prod_name[:35] + ('...' if len(prod_name) > 35 else ''))
        p.drawString(margin + 260, y, str(qty))
        p.drawRightString(width - margin, y, f'PKR {sales:,.2f}')
        y -= 12

    y -= 12
    p.setFont('Helvetica-Bold', 11)
    p.drawString(margin, y, 'Category Revenue Distribution')
    y -= 16

    p.setFont('Helvetica-Bold', 10)
    p.drawString(margin, y, 'Category')
    p.drawRightString(width - margin, y, 'Sales')
    y -= 14

    p.setFont('Helvetica', 9)
    for cat_name, sales in category_distribution:
        if y < margin + 60:
            p.showPage(); y = height - margin
            p.setFont('Helvetica-Bold', 10)
            p.drawString(margin, y, 'Category')
            p.drawRightString(width - margin, y, 'Sales')
            y -= 14
            p.setFont('Helvetica', 9)
        p.drawString(margin, y, cat_name)
        p.drawRightString(width - margin, y, f'PKR {sales:,.2f}')
        y -= 12

    p.save()
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name='website_analytics_report.pdf', mimetype='application/pdf')


@app.route('/admin/reports', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_reports():
    filter_type = request.args.get('filter', 'daily')
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')

    start, end = get_date_range(filter_type, start_date, end_date)

    orders = Order.query.filter(Order.created_at >= start, Order.created_at <= end)
    total_revenue = orders.with_entities(db.func.coalesce(db.func.sum(Order.total_price), 0.0)).scalar() or 0.0
    total_orders = orders.count()

    total_making_cost = db.session.query(db.func.coalesce(db.func.sum(OrderItem.making_cost * OrderItem.quantity), 0.0)).join(Order, Order.id == OrderItem.order_id).filter(Order.created_at >= start, Order.created_at <= end).scalar() or 0.0
    total_profit = total_revenue - total_making_cost
    total_due = orders.with_entities(db.func.coalesce(db.func.sum(Order.total_price - Order.amount_paid), 0.0)).scalar() or 0.0
    average_order_value = total_revenue / total_orders if total_orders > 0 else 0.0

    top_products = db.session.query(
        Product.name,
        db.func.coalesce(db.func.sum(OrderItem.quantity), 0).label('qty'),
        db.func.coalesce(db.func.sum(OrderItem.quantity * OrderItem.price), 0.0).label('total_sales')
    ).join(OrderItem, OrderItem.product_id == Product.id).join(Order, Order.id == OrderItem.order_id)
    top_products = top_products.filter(Order.created_at >= start, Order.created_at <= end).group_by(Product.id).order_by(db.desc('qty')).limit(5).all()

    category_distribution = db.session.query(
        Category.name,
        db.func.coalesce(db.func.sum(OrderItem.quantity * OrderItem.price), 0.0).label('sales')
    ).join(Product, Product.category_id == Category.id).join(OrderItem, OrderItem.product_id == Product.id).join(Order, Order.id == OrderItem.order_id)
    category_distribution = category_distribution.filter(Order.created_at >= start, Order.created_at <= end).group_by(Category.id).all()

    return render_template('admin/reports.html',
        filter_type=filter_type,
        start_date=start_date,
        end_date=end_date,
        total_revenue=total_revenue,
        total_profit=total_profit,
        total_due=total_due,
        total_orders=total_orders,
        average_order_value=average_order_value,
        total_cost=total_making_cost,
        top_products=top_products,
        category_distribution=category_distribution,
        sales_chart=url_for('admin_chart', chart_type='sales_trend', filter=filter_type, start_date=start_date, end_date=end_date),
        profit_chart=url_for('admin_chart', chart_type='profit_trend', filter=filter_type, start_date=start_date, end_date=end_date),
        orders_chart=url_for('admin_chart', chart_type='orders_trend', filter=filter_type, start_date=start_date, end_date=end_date),
        top_products_chart=url_for('admin_chart', chart_type='top_products', filter=filter_type, start_date=start_date, end_date=end_date),
        category_sales_chart=url_for('admin_chart', chart_type='category_sales', filter=filter_type, start_date=start_date, end_date=end_date)
    )


@app.route('/admin/reports/pdf')
@login_required
@admin_required
def admin_reports_pdf():
    if not HAS_REPORTLAB:
        flash('PDF generation is not available. Please install reportlab.', 'danger')
        return redirect(url_for('admin_reports'))
    
    filter_type = request.args.get('filter', 'daily')
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')

    start, end = get_date_range(filter_type, start_date, end_date)

    orders = Order.query.filter(Order.created_at >= start, Order.created_at <= end)
    total_revenue = orders.with_entities(db.func.coalesce(db.func.sum(Order.total_price), 0.0)).scalar() or 0.0
    total_orders = orders.count()

    total_making_cost = db.session.query(db.func.coalesce(db.func.sum(OrderItem.making_cost * OrderItem.quantity), 0.0)).join(Order, Order.id == OrderItem.order_id).filter(Order.created_at >= start, Order.created_at <= end).scalar() or 0.0
    total_profit = total_revenue - total_making_cost
    total_due = orders.with_entities(db.func.coalesce(db.func.sum(Order.total_price - Order.amount_paid), 0.0)).scalar() or 0.0
    average_order_value = total_revenue / total_orders if total_orders > 0 else 0.0

    complete_orders = orders.filter_by(status='Complete').count()
    pending_orders = orders.filter(Order.status.in_(['Pending', 'Pending Payment', 'Under Process', 'Awaiting Conversion'])).count()

    customer_dues = db.session.query(
        User.name,
        User.email,
        db.func.coalesce(db.func.sum(Order.total_price - Order.amount_paid), 0.0).label('dues')
    ).join(User, User.id == Order.user_id).filter(Order.created_at >= start, Order.created_at <= end).group_by(User.id).having(db.func.sum(Order.total_price - Order.amount_paid) > 0).order_by(db.desc('dues')).limit(10).all()

    top_products = db.session.query(
        Product.name,
        db.func.coalesce(db.func.sum(OrderItem.quantity), 0).label('qty'),
        db.func.coalesce(db.func.sum(OrderItem.quantity * OrderItem.price), 0.0).label('total_sales')
    ).join(OrderItem, OrderItem.product_id == Product.id).join(Order, Order.id == OrderItem.order_id)
    top_products = top_products.filter(Order.created_at >= start, Order.created_at <= end).group_by(Product.id).order_by(db.desc('qty')).limit(5).all()

    sales_image = generate_chart_image('sales_trend', start, end)
    profit_image = generate_chart_image('profit_trend', start, end)
    orders_image = generate_chart_image('orders_trend', start, end)
    category_sales_image = generate_chart_image('category_sales', start, end)
    category_profit_image = generate_chart_image('category_profit', start, end)
    order_status_image = generate_chart_image('order_status', start, end)
    profit_per_product_image = generate_chart_image('profit_per_product', start, end)

    status_rows = db.session.query(Order.status, db.func.count(Order.id)).filter(Order.created_at >= start, Order.created_at <= end).group_by(Order.status).all()
    status_summary = {r[0]: r[1] for r in status_rows}

    buf = io.BytesIO()
    p = canvas.Canvas(buf, pagesize=letter)
    p.setTitle('Admin Reports PDF')

    # Header
    p.setFillColorRGB(0.12, 0.27, 0.36)
    p.rect(0, 740, 612, 70, fill=True, stroke=False)
    p.setFillColorRGB(1, 1, 1)
    p.setFont('Helvetica-Bold', 18)
    p.drawString(40, 760, 'Sugar Blush Bakery - Reports & Analytics')
    p.setFont('Helvetica', 10)
    p.drawString(40, 744, f'Filter: {filter_type.title()} | Date Range: {start_date or "-"} to {end_date or "-"}')

    # Key metric cards
    metrics = [
        ('Total Revenue', f'PKR {total_revenue:,.2f}', (0.18, 0.57, 0.84)),
        ('Total Profit', f'PKR {total_profit:,.2f}', (0.11, 0.62, 0.2)),
        ('Total Cost', f'PKR {total_making_cost:,.2f}', (0.5, 0.5, 0.5)),
        ('Total Due', f'PKR {total_due:,.2f}', (0.8, 0.35, 0.35)),
        ('Total Orders', str(total_orders), (0.95, 0.68, 0.2)),
        ('Complete Orders', str(complete_orders), (0.0, 0.48, 0.65)),
        ('Pending Orders', str(pending_orders), (0.85, 0.55, 0.0)),
        ('Avg Order Value', f'PKR {average_order_value:,.2f}', (0.35, 0.43, 0.68))
    ]

    x = 40
    y = 660
    card_w = 135
    card_h = 45
    for i, (label, value, color) in enumerate(metrics):
        p.setFillColorRGB(*color)
        p.roundRect(x, y, card_w, card_h, 8, fill=True, stroke=False)
        p.setFillColorRGB(1, 1, 1)
        p.setFont('Helvetica-Bold', 8)
        p.drawString(x + 8, y + 30, label)
        p.setFont('Helvetica-Bold', 10)
        p.drawString(x + 8, y + 14, value)
        x += card_w + 10
        if x + card_w > 560:
            x = 40
            y -= card_h + 10

    # ensure space before section details
    y -= 40
    if y < 120:
        p.showPage()
        y = 760

    # Order status and product summary
    p.setFillColorRGB(0, 0, 0)
    p.setFont('Helvetica-Bold', 12)
    p.drawString(40, y, 'Order Status Breakdown')
    y -= 18
    p.setFillColorRGB(0, 0, 0)
    p.setFont('Helvetica-Bold', 12)
    p.drawString(40, y, 'Order Status Breakdown')
    y -= 16
    p.setFont('Helvetica', 10)
    for status, count in status_summary.items():
        p.drawString(45, y, f'- {status}: {count}')
        y -= 12

    y -= 8
    p.setFont('Helvetica-Bold', 12)
    p.drawString(40, y, 'Top 5 Products')
    y -= 16
    p.setFont('Helvetica', 10)
    if top_products:
        for idx, p_item in enumerate(top_products, 1):
            p.drawString(45, y, f'{idx}. {p_item.name} | Qty: {p_item.qty} | Sales: PKR {p_item.total_sales:,.2f}')
            y -= 12
            if y < 130:
                p.showPage(); p.setFont('Helvetica', 10); y = 760
    else:
        p.drawString(45, y, 'No product sales in this date range.')
        y -= 14

    y -= 8
    p.setFont('Helvetica-Bold', 12)
    p.drawString(40, y, 'Top 10 Customer Dues')
    y -= 16
    p.setFont('Helvetica-Bold', 10)
    p.drawString(45, y, 'No.  Customer (Email)                                       Dues')
    y -= 14
    p.setFont('Helvetica', 10)
    if customer_dues:
        for idx, c in enumerate(customer_dues, 1):
            if y < 110:
                p.showPage(); p.setFont('Helvetica', 10); y = 760
            line = f'{idx:>2}. {c.name} ({c.email})'
            p.drawString(45, y, line)
            p.drawRightString(560, y, f'PKR {c.dues:,.2f}')
            y -= 12
    else:
        p.drawString(45, y, 'No due customers in selected range.')
        y -= 14

    # Page 2 - graphs
    p.showPage()
    p.setFont('Helvetica-Bold', 14)
    p.drawString(40, 760, 'Sales and Profit Analytics')
    p.drawImage(ImageReader(sales_image), 40, 520, width=260, height=180)
    p.drawImage(ImageReader(profit_image), 310, 520, width=260, height=180)

    p.drawString(40, 500, 'Orders Trend')
    p.drawImage(ImageReader(orders_image), 40, 310, width=260, height=170)
    p.drawString(310, 500, 'Category Sales Distribution')
    p.drawImage(ImageReader(category_sales_image), 310, 310, width=260, height=170)

    # Page 3 - More charts
    p.showPage()
    p.setFont('Helvetica-Bold', 14)
    p.drawString(40, 760, 'Category Profit & Order Status')
    p.drawImage(ImageReader(category_profit_image), 40, 520, width=260, height=180)
    p.drawImage(ImageReader(order_status_image), 310, 520, width=260, height=180)

    p.drawString(40, 500, 'Top Profit Products')
    p.drawImage(ImageReader(profit_per_product_image), 40, 310, width=520, height=170)

    p.save()
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name='admin_report.pdf', mimetype='application/pdf')

@app.route('/admin/chart/<chart_type>')
@login_required
@admin_required
def admin_chart(chart_type):
    filter_type = request.args.get('filter', 'daily')
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')
    start, end = get_date_range(filter_type, start_date, end_date)

    chart_buf = generate_chart_image(chart_type, start, end)
    return send_file(chart_buf, mimetype='image/png')


@app.route('/admin/contact_messages')
@login_required
@admin_required
def admin_contact_messages():
    messages = ContactMessage.query.order_by(ContactMessage.created_at.desc()).all()
    return render_template('admin/contact_messages.html', messages=messages)


@app.route('/admin/contact_messages/<int:msg_id>', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_contact_message_detail(msg_id):
    message = ContactMessage.query.get_or_404(msg_id)
    if request.method == 'POST':
        response = request.form.get('response')
        status = request.form.get('status')
        message.response = response
        message.status = status or 'Replied'
        db.session.commit()
        flash('Contact message updated.', 'success')
        return redirect(url_for('admin_contact_messages'))
    return render_template('admin/contact_message_detail.html', message=message)


@app.route('/admin/users')
@login_required
@admin_required
def admin_users():
    user_name = request.args.get('user_name', '').strip()
    users_q = User.query.order_by(User.created_at.desc())
    if user_name:
        users_q = users_q.filter((User.name.ilike(f'%{user_name}%')) | (User.email.ilike(f'%{user_name}%')))

    users = users_q.all()
    user_dues = {}
    due_warning = {}

    setting = SiteSetting.query.filter_by(key='due_warning_threshold').first()
    due_threshold = float(setting.value) if setting and setting.value else 0.0

    for u in users:
        if u.role == 'admin':
            user_dues[u.id] = 0.0
            due_warning[u.id] = False
            continue

        user_due = get_user_complete_due(u.id)
        user_dues[u.id] = user_due
        due_warning[u.id] = (due_threshold > 0.0 and user_due >= due_threshold)

        if due_warning[u.id]:
            # auto-send warning record if threshold crossed
            ensure_user_due_warning(u)

    return render_template('admin/users.html', users=users, user_dues=user_dues, due_warning=due_warning, due_threshold=due_threshold)

@app.route('/admin/users/<int:user_id>/suspend')
@login_required
@admin_required
def suspend_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.role == 'admin':
        flash('Cannot suspend admin', 'danger')
        return redirect(url_for('admin_users'))
    user.role = 'suspended'
    db.session.commit()
    flash('User suspended', 'warning')
    return redirect(url_for('admin_users'))

@app.route('/admin/users/<int:user_id>/unsuspend')
@login_required
@admin_required
def unsuspend_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.role == 'admin':
        flash('Admin cannot be unsuspended', 'info')
        return redirect(url_for('admin_users'))
    user.role = 'user'
    db.session.commit()
    flash('User unsuspended', 'success')
    return redirect(url_for('admin_users'))

@app.route('/admin/users/<int:user_id>/warn', methods=['POST'])
@login_required
@admin_required
def warn_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.role == 'admin':
        flash('Cannot send warning to admin.', 'info')
        return redirect(url_for('admin_users'))

    due_amount = db.session.query(
        db.func.coalesce(db.func.sum(Order.total_price - Order.amount_paid), 0.0)
    ).filter(Order.user_id == user.id).scalar() or 0.0

    setting = SiteSetting.query.filter_by(key='due_warning_threshold').first()
    due_threshold = float(setting.value) if setting and setting.value else 0.0

    warning_msg = f"Dear {user.name}, your current outstanding due is PKR {due_amount:.2f}."
    if due_threshold > 0 and due_amount >= due_threshold:
        warning_msg += " Please settle your invoice at the earliest to avoid account restrictions."
    else:
        warning_msg += " No warning level reached, this is an informational note."

    # record admin warning as contact message for auditing
    warning_record = ContactMessage(
        name=user.name,
        email=user.email or 'noreply@sugarblush.local',
        phone=user.phone1 or '',
        message=warning_msg,
        status='Warning Sent'
    )
    db.session.add(warning_record)
    db.session.commit()

    flash('Warning note recorded and dispatched to user.', 'success')
    return redirect(url_for('admin_users'))


@app.route('/admin/users/<int:user_id>/record_payment', methods=['POST'])
@login_required
@admin_required
def record_user_payment(user_id):
    user = User.query.get_or_404(user_id)
    if user.role == 'admin':
        flash('Cannot record payment for admin.', 'danger')
        return redirect(url_for('admin_users'))

    paid_amount = float(request.form.get('paid_amount', 0) or 0)
    if paid_amount <= 0:
        flash('Payment amount must be greater than zero.', 'danger')
        return redirect(url_for('admin_users'))

    due_orders = Order.query.filter(
        Order.user_id == user.id,
        Order.total_price > Order.amount_paid
    ).order_by(Order.created_at.asc()).all()

    remaining = paid_amount
    for order in due_orders:
        if remaining <= 0:
            break
        order_due = order.total_price - order.amount_paid
        allocate = min(order_due, remaining)
        order.amount_paid += allocate
        remaining -= allocate
        if order.amount_paid >= order.total_price:
            order.status = 'Complete'
        elif order.amount_paid > 0 and order.status == 'Pending Payment':
            order.status = 'Under Process'

    if remaining > 0:
        flash(f'Paid PKR {paid_amount:.2f} recorded; PKR {remaining:.2f} remaining could not be applied (no due orders).', 'warning')
    else:
        flash(f'Paid PKR {paid_amount:.2f} recorded and applied to due orders.', 'success')

    db.session.commit()
    return redirect(url_for('admin_users'))


@app.route('/admin/users/<int:user_id>/delete')
@login_required
@admin_required
def delete_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.role == 'admin':
        flash('Cannot delete admin', 'danger')
        return redirect(url_for('admin_users'))
    orders = Order.query.filter_by(user_id=user.id).all()
    for order in orders:
        OrderItem.query.filter_by(order_id=order.id).delete()
        AddOn.query.filter_by(order_id=order.id).delete()
        db.session.delete(order)
    CustomOrder.query.filter_by(user_id=user.id).delete()
    db.session.delete(user)
    db.session.commit()
    flash('User and related data deleted', 'danger')
    return redirect(url_for('admin_users'))

@app.route('/admin/users/<int:user_id>/orders')
@login_required
@admin_required
def admin_user_orders(user_id):
    user = User.query.get_or_404(user_id)
    orders = Order.query.filter_by(user_id=user.id).order_by(Order.created_at.desc()).all()
    total_due = sum((order.total_price - order.amount_paid) for order in orders)

    total_orders = len(orders)
    completed_orders = sum(1 for o in orders if o.status.lower() == 'complete')
    pending_orders = total_orders - completed_orders
    total_amount = sum(order.total_price for order in orders)
    total_paid = sum(order.amount_paid for order in orders)
    total_making_cost = sum(item.making_cost * item.quantity for order in orders for item in order.items)
    total_profit = sum(item.profit for order in orders for item in order.items)

    return render_template(
        'admin/user_orders.html',
        user=user,
        orders=orders,
        total_due=total_due,
        total_orders=total_orders,
        completed_orders=completed_orders,
        pending_orders=pending_orders,
        total_amount=total_amount,
        total_paid=total_paid,
        total_making_cost=total_making_cost,
        total_profit=total_profit
    )


@app.route('/admin/users/<int:user_id>/report_pdf')
@login_required
@admin_required
def admin_user_report_pdf(user_id):
    if not HAS_REPORTLAB:
        flash('PDF report functionality is unavailable. reportlab is not installed.', 'danger')
        return redirect(url_for('admin_user_orders', user_id=user_id))

    user = User.query.get_or_404(user_id)
    orders = Order.query.filter_by(user_id=user.id).order_by(Order.created_at.desc()).all()

    total_orders = len(orders)
    completed_orders = sum(1 for o in orders if o.status.lower() == 'complete')
    pending_orders = total_orders - completed_orders
    total_amount = sum(order.total_price for order in orders)
    total_paid = sum(order.amount_paid for order in orders)
    total_due = total_amount - total_paid
    total_making_cost = sum(item.making_cost * item.quantity for order in orders for item in order.items)
    total_profit = sum(item.profit for order in orders for item in order.items)

    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    import io

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter
    margin = 40
    y = height - margin
    page_number = 1

    def draw_page_footer():
        nonlocal pdf, page_number
        pdf.setFont('Helvetica', 8)
        pdf.setFillColor(colors.grey)
        pdf.drawRightString(width - margin, 15, f'Page {page_number}')
        pdf.setFillColor(colors.black)

    def draw_header():
        nonlocal pdf, y
        pdf.setFont('Helvetica-Bold', 18)
        pdf.setFillColor(colors.HexColor('#8d3d3e'))
        pdf.drawString(margin, y, 'SUGAR BLUSH - Customer Invoice Report')
        y -= 22
        pdf.setFont('Helvetica', 10)
        pdf.setFillColor(colors.black)
        pdf.drawString(margin, y, f'User: {user.name}  |  Email: {user.email or "N/A"}')
        y -= 14
        pdf.drawString(margin, y, f'Report Date: {datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}')
        y -= 18
        pdf.setStrokeColor(colors.HexColor('#e27f74'))
        pdf.setLineWidth(1.2)
        pdf.line(margin, y, width - margin, y)
        y -= 20

    def new_page():
        nonlocal y, page_number
        draw_page_footer()
        pdf.showPage()
        page_number += 1
        y = height - margin
        draw_header()

    draw_header()

    pdf.setFont('Helvetica-Bold', 12)
    pdf.drawString(margin, y, 'Overall Summary')
    y -= 16
    pdf.setFont('Helvetica', 10)
    summary_items = [
        ('Total Orders', total_orders),
        ('Completed Orders', completed_orders),
        ('Pending Orders', pending_orders),
        ('Total Sales', f'PKR {total_amount:.2f}'),
        ('Total Paid', f'PKR {total_paid:.2f}'),
        ('Total Due', f'PKR {total_due:.2f}'),
        ('Total Production Cost', f'PKR {total_making_cost:.2f}'),
        ('Total Profit', f'PKR {total_profit:.2f}')
    ]
    for label, value in summary_items:
        pdf.drawString(margin, y, f'{label}: {value}')
        y -= 14

    y -= 10
    pdf.setFont('Helvetica-Bold', 12)
    if y < 120:
        new_page()
    pdf.drawString(margin, y, 'Invoice Details (per order)')
    y -= 18

    for order in orders:
        if y < 180:
            new_page()

        pdf.setFont('Helvetica-Bold', 11)
        pdf.setFillColor(colors.HexColor('#333333'))
        pdf.drawString(margin, y, f'Order #{order.order_number or order.id} - {order.status}')
        y -= 14

        pdf.setFont('Helvetica', 9)
        pdf.setFillColor(colors.black)
        customer_address = []
        if order.user.address1: customer_address.append(order.user.address1)
        if order.user.address2: customer_address.append(order.user.address2)
        if order.user.city: customer_address.append(order.user.city)
        if order.user.state: customer_address.append(order.user.state)
        if order.user.postal_code: customer_address.append(order.user.postal_code)
        address = ', '.join(customer_address)

        pdf.drawString(margin, y, f'Date: {order.created_at.strftime("%Y-%m-%d %H:%M") if order.created_at else "N/A"}')
        pdf.drawString(width/2, y, f'Payment: {order.payment_method or "Cash on Delivery"}')
        y -= 12
        pdf.drawString(margin, y, f'Customer: {order.user.name} ({order.user.email})')
        y -= 12
        if address:
            pdf.drawString(margin, y, f'Address: {address}')
            y -= 12

        pdf.setFont('Helvetica-Bold', 9)
        pdf.setFillColor(colors.white)
        pdf.setStrokeColor(colors.HexColor('#8d3d3e'))
        pdf.setFillColor(colors.HexColor('#8d3d3e'))
        pdf.rect(margin, y-2, width - 2*margin, 18, fill=1, stroke=0)
        pdf.setFillColor(colors.white)
        pdf.drawString(margin + 4, y + 2, 'Item')
        pdf.drawRightString(width - margin - 180, y + 2, 'Qty')
        pdf.drawRightString(width - margin - 120, y + 2, 'Unit Price')
        pdf.drawRightString(width - margin - 60, y + 2, 'Line Total')
        y -= 22

        pdf.setFont('Helvetica', 8)
        pdf.setFillColor(colors.black)

        order_line_total = 0.0
        order_cost_total = 0.0
        order_profit_total = 0.0
        for item in order.items:
            if y < 70:
                new_page();
            item_name = item.product.name if item.product else 'Custom item'
            line_price = item.price * item.quantity
            order_line_total += line_price
            order_cost_total += item.making_cost * item.quantity
            order_profit_total += item.profit
            pdf.drawString(margin + 2, y, f'{item_name}')
            pdf.drawRightString(width - margin - 180, y, f'{item.quantity}')
            pdf.drawRightString(width - margin - 120, y, f'PKR {item.price:.2f}')
            pdf.drawRightString(width - margin - 60, y, f'PKR {line_price:.2f}')
            y -= 12

        for addon in order.addons:
            if y < 70:
                new_page()
            addon_price = addon.price
            order_line_total += addon_price
            pdf.drawString(margin + 2, y, f'Addon: {addon.name}')
            pdf.drawRightString(width - margin - 180, y, '1')
            pdf.drawRightString(width - margin - 120, y, f'PKR {addon_price:.2f}')
            pdf.drawRightString(width - margin - 60, y, f'PKR {addon_price:.2f}')
            y -= 12

        pdf.setLineWidth(0.5)
        pdf.setStrokeColor(colors.grey)
        pdf.line(margin, y, width - margin, y)
        y -= 10

        pdf.setFont('Helvetica-Bold', 9)
        pdf.drawRightString(width - margin - 60, y, f'Order Amount: PKR {order.total_price:.2f}')
        y -= 12
        pdf.drawRightString(width - margin - 60, y, f'Paid: PKR {order.amount_paid:.2f}')
        y -= 12
        pdf.drawRightString(width - margin - 60, y, f'Due: PKR {order.total_price - order.amount_paid:.2f}')
        y -= 14

        pdf.drawString(margin, y, f'Order cost: PKR {order_cost_total:.2f} | Order profit: PKR {order_profit_total:.2f}')
        y -= 18

        pdf.setStrokeColor(colors.HexColor('#e27f74'))
        pdf.line(margin, y, width - margin, y)
        y -= 12

    draw_page_footer()
    pdf.save()
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name=f'user_{user.id}_invoice_report.pdf', mimetype='application/pdf')

@app.route('/admin/products', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_products():
    if request.method == 'POST':
        name = request.form['name']
        desc = request.form['description']
        making_cost = float(request.form['making_cost'])
        sale_price = float(request.form['sale_price'])
        discount = float(request.form['discount'])
        stock = max(0, int(request.form.get('stock', 0)))
        category_id = int(request.form['category_id'])
        is_active = bool(request.form.get('is_active', 'on'))

        if sale_price < making_cost:
            flash('Sale price cannot be lower than making cost.', 'danger')
            return redirect(url_for('admin_products'))

        image_url = request.form.get('image', '').strip()
        image_file = request.files.get('image_file')
        final_image = None

        if image_file and image_file.filename:
            filename = secure_filename(image_file.filename)
            path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            image_file.save(path)
            final_image = url_for('static', filename=f'uploads/{filename}')
        else:
            final_image = image_url or 'https://via.placeholder.com/400x300?text=Product'

        product = Product(name=name, description=desc, price=sale_price, sale_price=sale_price, making_cost=making_cost, discount=discount, stock=stock, category_id=category_id, image=final_image, is_active=is_active)
        db.session.add(product)
        db.session.commit()
        flash('Product added.', 'success')
        return redirect(url_for('admin_products'))

    product_name = request.args.get('product_name', '').strip()
    products_q = Product.query.order_by(Product.created_at.desc())
    if product_name:
        products_q = products_q.filter(Product.name.ilike(f'%{product_name}%'))
    products = products_q.all()

    categories = Category.query.all()
    return render_template('admin/products.html', products=products, categories=categories, product_name=product_name)

@app.route('/admin/product_addons', methods=['POST'])
@login_required
@admin_required
def admin_product_addons():
    product_id = int(request.form.get('product_id'))
    name = request.form.get('addon_name')
    price = float(request.form.get('addon_price', 0))
    image_url = request.form.get('addon_image', '').strip()
    file = request.files.get('addon_image_file')

    final_image = None
    if file and file.filename:
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        final_image = url_for('static', filename=f'uploads/{filename}')
    else:
        final_image = image_url or 'https://via.placeholder.com/150?text=AddOn'

    addon = ProductAddOn(product_id=product_id, name=name, price=price, image=final_image)
    db.session.add(addon)
    db.session.commit()
    flash('Product add-on created.', 'success')
    return redirect(url_for('admin_products'))

@app.route('/admin/product_addons/<int:product_id>')
@login_required
@admin_required
def admin_get_product_addons(product_id):
    addons = ProductAddOn.query.filter_by(product_id=product_id).all()
    return jsonify([{'id':a.id, 'name':a.name, 'price':a.price} for a in addons])

@app.route('/admin/products/<int:product_id>/edit', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_product(product_id):
    product = Product.query.get_or_404(product_id)
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip()
        category_id = request.form.get('category_id')
        sale_price = float(request.form['sale_price'])
        making_cost = float(request.form['making_cost'])
        discount = float(request.form['discount'])

        if not name:
            flash('Product name is required.', 'danger')
            return redirect(url_for('edit_product', product_id=product_id))

        if not description:
            flash('Product description is required.', 'danger')
            return redirect(url_for('edit_product', product_id=product_id))

        if sale_price < making_cost:
            flash('Sale price cannot be lower than making cost.', 'danger')
            return redirect(url_for('edit_product', product_id=product_id))

        product.name = name
        product.description = description

        if category_id:
            try:
                product.category_id = int(category_id)
            except ValueError:
                product.category_id = None

        image_url = request.form.get('image', '').strip()
        image_file = request.files.get('image_file')

        if image_file and image_file.filename:
            filename = secure_filename(image_file.filename)
            path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            image_file.save(path)
            product.image = url_for('static', filename=f'uploads/{filename}')
        elif image_url:
            product.image = image_url

        product.price = sale_price
        product.sale_price = sale_price
        product.making_cost = making_cost
        product.discount = discount
        product.stock = max(0, int(request.form.get('stock', product.stock or 0)))
        product.is_active = bool(request.form.get('is_active', 'off'))
        db.session.commit()
        flash('Product updated.', 'success')
        return redirect(url_for('admin_products'))
    
    categories = Category.query.order_by(Category.name).all()
    return render_template('admin/edit_product.html', product=product, categories=categories)

@app.route('/admin/products/<int:product_id>/toggle_active')
@login_required
@admin_required
def toggle_product_active(product_id):
    product = Product.query.get_or_404(product_id)
    product.is_active = not bool(product.is_active)
    db.session.commit()
    flash(f"Product {'enabled' if product.is_active else 'disabled'} on website.", 'success')
    return redirect(url_for('admin_products'))


@app.route('/admin/products/<int:product_id>/delete')
@login_required
@admin_required
def delete_product(product_id):
    product = Product.query.get_or_404(product_id)

    # delete related addons first to avoid nullable product_id update
    ProductAddOn.query.filter_by(product_id=product.id).delete()

    db.session.delete(product)
    db.session.commit()
    flash('Product deleted.', 'info')
    return redirect(url_for('admin_products'))

@app.route('/admin/categories', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_categories():
    if request.method == 'POST':
        name = request.form['name']
        if not Category.query.filter_by(name=name).first():
            c = Category(name=name)
            db.session.add(c)
            db.session.commit()
            flash('Category added.', 'success')
        else:
            flash('Category already exists.', 'warning')
    categories = Category.query.all()
    return render_template('admin/categories.html', categories=categories)

@app.route('/admin/categories/<int:category_id>/delete')
@login_required
@admin_required
def delete_category(category_id):
    cat = Category.query.get_or_404(category_id)
    db.session.delete(cat)
    db.session.commit()
    flash('Category deleted.', 'info')
    return redirect(url_for('admin_categories'))

@app.route('/admin/banners', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_banners():
    if request.method == 'POST':
        image_url = request.form.get('image', '').strip()
        file = request.files.get('image_file')
        final_image = None
        if file and file.filename:
            filename = secure_filename(file.filename)
            save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(save_path)
            final_image = url_for('static', filename=f'uploads/{filename}')
        else:
            final_image = image_url or 'https://via.placeholder.com/1200x400?text=Promo'

        category_id = request.form.get('category_id') or None
        active = bool(request.form.get('active'))
        b = PromotionBanner(image=final_image, category_id=category_id, active=active)
        db.session.add(b)
        db.session.commit()
        flash('Banner added.', 'success')
        return redirect(url_for('admin_banners'))
    banners = PromotionBanner.query.all()
    categories = Category.query.all()
    return render_template('admin/banners.html', banners=banners, categories=categories)

@app.route('/admin/banners/<int:banner_id>/toggle')
@login_required
@admin_required
def toggle_banner(banner_id):
    b = PromotionBanner.query.get_or_404(banner_id)
    b.active = not b.active
    db.session.commit()
    flash('Banner toggled.', 'info')
    return redirect(url_for('admin_banners'))

@app.route('/admin/banners/<int:banner_id>/delete')
@login_required
@admin_required
def delete_banner(banner_id):
    b = PromotionBanner.query.get_or_404(banner_id)
    db.session.delete(b)
    db.session.commit()
    flash('Banner deleted.', 'info')
    return redirect(url_for('admin_banners'))

@app.route('/admin/orders/<int:order_id>/cancel', methods=['POST'])
@login_required
@admin_required
def cancel_order(order_id):
    order = Order.query.get_or_404(order_id)
    order.status = 'Cancelled'
    db.session.commit()
    flash('Order cancelled.', 'warning')
    return redirect(url_for('admin_orders'))

@app.route('/admin/orders/<int:order_id>/delete', methods=['POST'])
@login_required
@admin_required
def delete_order(order_id):
    order = Order.query.get_or_404(order_id)
    OrderItem.query.filter_by(order_id=order.id).delete()
    AddOn.query.filter_by(order_id=order.id).delete()
    db.session.delete(order)
    db.session.commit()
    flash('Order removed.', 'info')
    return redirect(url_for('admin_orders'))

@app.route('/admin/site-settings', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_site_settings():
    from models import SiteSetting
    setting = SiteSetting.query.filter_by(key='site_logo').first()
    if request.method == 'POST':
        logo_url = request.form.get('logo', '').strip()
        logo_file = request.files.get('logo_file')
        final_logo = None

        if logo_file and logo_file.filename:
            filename = secure_filename(logo_file.filename)
            save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            logo_file.save(save_path)
            final_logo = url_for('static', filename=f'uploads/{filename}')
        elif logo_url:
            final_logo = logo_url

        if final_logo:
            if not setting:
                setting = SiteSetting(key='site_logo', value=final_logo)
                db.session.add(setting)
            else:
                setting.value = final_logo
            db.session.commit()
            flash('Site logo updated.', 'success')
        else:
            flash('Please provide a logo URL or upload an image file.', 'warning')

        bank_details = request.form.get('bank_details', '').strip()
        if bank_details:
            bank_setting = SiteSetting.query.filter_by(key='bank_details').first()
            if not bank_setting:
                bank_setting = SiteSetting(key='bank_details', value=bank_details)
                db.session.add(bank_setting)
            else:
                bank_setting.value = bank_details
            db.session.commit()
            flash('Bank details updated.', 'success')

        social_facebook = request.form.get('social_facebook', '').strip()
        social_instagram = request.form.get('social_instagram', '').strip()
        social_youtube = request.form.get('social_youtube', '').strip()
        social_tiktok = request.form.get('social_tiktok', '').strip()

        about_text = request.form.get('about_text', '').strip()
        about_image_url = request.form.get('about_image', '').strip()
        about_image_file = request.files.get('about_image_file')

        for key, value in [('social_facebook', social_facebook), ('social_instagram', social_instagram), ('social_youtube', social_youtube), ('social_tiktok', social_tiktok), ('about_text', about_text)]:
            if value:
                item = SiteSetting.query.filter_by(key=key).first()
                if not item:
                    item = SiteSetting(key=key, value=value)
                    db.session.add(item)
                else:
                    item.value = value
                db.session.commit()

        if about_image_file and about_image_file.filename:
            filename = secure_filename(about_image_file.filename)
            save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            about_image_file.save(save_path)
            about_image_final = url_for('static', filename=f'uploads/{filename}')
        elif about_image_url:
            about_image_final = about_image_url
        else:
            about_image_final = None

        if about_image_final:
            about_image_setting = SiteSetting.query.filter_by(key='about_image').first()
            if not about_image_setting:
                about_image_setting = SiteSetting(key='about_image', value=about_image_final)
                db.session.add(about_image_setting)
            else:
                about_image_setting.value = about_image_final
            db.session.commit()

        due_warning_threshold = request.form.get('due_warning_threshold', '').strip()
        if due_warning_threshold:
            try:
                threshold_val = float(due_warning_threshold)
                due_warning_setting = SiteSetting.query.filter_by(key='due_warning_threshold').first()
                if not due_warning_setting:
                    due_warning_setting = SiteSetting(key='due_warning_threshold', value=str(threshold_val))
                    db.session.add(due_warning_setting)
                else:
                    due_warning_setting.value = str(threshold_val)
                db.session.commit()
                flash('Due warning threshold updated.', 'success')
            except ValueError:
                flash('Due warning threshold must be a number.', 'danger')

        stock_warning_threshold = request.form.get('stock_warning_threshold', '').strip()
        if stock_warning_threshold:
            try:
                stock_threshold_val = int(stock_warning_threshold)
                stock_warning_setting = SiteSetting.query.filter_by(key='stock_warning_threshold').first()
                if not stock_warning_setting:
                    stock_warning_setting = SiteSetting(key='stock_warning_threshold', value=str(stock_threshold_val))
                    db.session.add(stock_warning_setting)
                else:
                    stock_warning_setting.value = str(stock_threshold_val)
                db.session.commit()
                flash('Stock warning threshold updated.', 'success')
            except ValueError:
                flash('Stock warning threshold must be an integer.', 'danger')

        return redirect(url_for('admin_site_settings'))

    bank_setting = SiteSetting.query.filter_by(key='bank_details').first()
    due_warning_setting = SiteSetting.query.filter_by(key='due_warning_threshold').first()
    stock_warning_setting = SiteSetting.query.filter_by(key='stock_warning_threshold').first()
    social_facebook_setting = SiteSetting.query.filter_by(key='social_facebook').first()
    social_instagram_setting = SiteSetting.query.filter_by(key='social_instagram').first()
    social_youtube_setting = SiteSetting.query.filter_by(key='social_youtube').first()
    social_tiktok_setting = SiteSetting.query.filter_by(key='social_tiktok').first()
    about_image_setting = SiteSetting.query.filter_by(key='about_image').first()
    about_text_setting = SiteSetting.query.filter_by(key='about_text').first()

    due_warning_threshold = due_warning_setting.value if due_warning_setting else ''
    stock_warning_threshold = stock_warning_setting.value if stock_warning_setting else ''
    return render_template('admin/site_settings.html',
        setting=setting,
        bank_setting=bank_setting,
        due_warning_threshold=due_warning_threshold,
        stock_warning_threshold=stock_warning_threshold,
        social_facebook=social_facebook_setting.value if social_facebook_setting else '',
        social_instagram=social_instagram_setting.value if social_instagram_setting else '',
        social_youtube=social_youtube_setting.value if social_youtube_setting else '',
        social_tiktok=social_tiktok_setting.value if social_tiktok_setting else '',
        about_image=about_image_setting.value if about_image_setting else '',
        about_text=about_text_setting.value if about_text_setting else ''
    )

@app.route('/admin/announcements', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_announcements():
    if request.method == 'POST':
        title = request.form['title']
        content = request.form['content']
        a = Announcement(title=title, content=content)
        db.session.add(a)
        db.session.commit()
        flash('Announcement added.', 'success')
    ann = Announcement.query.order_by(Announcement.created_at.desc()).all()
    return render_template('admin/announcements.html', announcements=ann)

@app.route('/admin/announcements/<int:id>/delete')
@login_required
@admin_required
def delete_announcement(id):
    ann = Announcement.query.get_or_404(id)
    db.session.delete(ann)
    db.session.commit()
    flash('Announcement deleted.', 'info')
    return redirect(url_for('admin_announcements'))

@app.route('/admin/orders', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_orders():
    if request.method == 'POST':
        user_id = int(request.form.get('user_id', 0))
        payment_method = request.form.get('payment_method', 'Cash on Delivery')

        if user_id == 0:
            customer_name = request.form.get('customer_name', '').strip()
            customer_email = request.form.get('customer_email', '').strip()
            if not customer_name:
                flash('Please provide a name for the new customer.', 'danger')
                return redirect(url_for('admin_orders'))

            if not customer_email:
                # Generate fallback unique email for system user, avoid unique constraint collisions
                sanitized = ''.join(c for c in customer_name.lower() if c.isalnum()) or 'user'
                candidate = f"{sanitized}@sugarblush.local"
                attempt = 1
                while User.query.filter_by(email=candidate).first():
                    candidate = f"{sanitized}{attempt}@sugarblush.local"
                    attempt += 1
                customer_email = candidate

            user = User.query.filter_by(email=customer_email).first()
            if not user:
                user = User(name=customer_name, email=customer_email)
                user.set_password('temp1234')
                db.session.add(user)
                db.session.flush()
            user_id = user.id

        product_ids = request.form.getlist('product_id')
        quantities = request.form.getlist('quantity')
        manual_names = request.form.getlist('manual_name')
        manual_descriptions = request.form.getlist('manual_description')
        manual_sale_prices = request.form.getlist('manual_sale_price')
        manual_making_costs = request.form.getlist('manual_making_cost')

        if not product_ids or not quantities or len(product_ids) != len(quantities):
            flash('Please add at least one product with quantity.', 'danger')
            return redirect(url_for('admin_orders'))

        order_items_data = []
        total_price = 0.0
        for idx, pid in enumerate(product_ids):
            if not pid:
                continue
            try:
                quantity = int(quantities[idx])
            except (IndexError, ValueError):
                quantity = 1
            if quantity < 1:
                continue

            product = None
            item_price = 0.0
            making_cost = 0.0
            order_item_name = ''

            if pid == 'manual':
                manual_name = manual_names[idx] if idx < len(manual_names) else ''
                if not manual_name.strip():
                    continue
                order_item_name = manual_name.strip()
                manual_desc = manual_descriptions[idx] if idx < len(manual_descriptions) else ''
                try:
                    item_price = float(manual_sale_prices[idx]) if idx < len(manual_sale_prices) and manual_sale_prices[idx] else 0.0
                except (IndexError, ValueError):
                    item_price = 0.0
                try:
                    making_cost = float(manual_making_costs[idx]) if idx < len(manual_making_costs) and manual_making_costs[idx] else 0.0
                except (IndexError, ValueError):
                    making_cost = 0.0

                product = Product(name=order_item_name,
                                  description=manual_desc or order_item_name,
                                  category_id=None,
                                  image='',
                                  price=item_price,
                                  sale_price=item_price,
                                  making_cost=making_cost)
                db.session.add(product)
                db.session.flush()
            else:
                try:
                    product = Product.query.get(int(pid))
                except ValueError:
                    product = None

            if not product:
                continue

            if pid != 'manual':
                item_price = product.sale_price
                making_cost = product.making_cost
                order_item_name = product.name

            item_total = item_price * quantity
            total_price += item_total
            order_items_data.append((product, quantity, item_price, making_cost, order_item_name))

        if not order_items_data:
            flash('Please include at least one valid product in order.', 'danger')
            return redirect(url_for('admin_orders'))

        addon_ids = request.form.getlist('addon_ids')
        addon_total = 0.0
        selected_addons = []
        for addon_id in addon_ids:
            if addon_id:
                p_addon = ProductAddOn.query.get(int(addon_id))
                if p_addon:
                    addon_total += p_addon.price
                    selected_addons.append(p_addon)

        total_price += addon_total

        payment_details = ''
        if payment_method == 'Online':
            bank_setting = SiteSetting.query.filter_by(key='bank_details').first()
            payment_details = bank_setting.value if bank_setting else 'Bank details not set.'

        order_name = 'Multiple items' if len(order_items_data) > 1 or (len(order_items_data) == 1 and order_items_data[0][1] > 1) else order_items_data[0][4]
        if order_name == 'Multiple items':
            order_name = ', '.join(f"{item_name} x{qty}" for _, qty, _, _, item_name in order_items_data)

        order = Order(
            user_id=user_id,
            total_price=total_price,
            amount_paid=0.0,
            status='Under Process',
            order_number=generate_order_number(),
            order_name=order_name,
            payment_method=payment_method,
            payment_details=payment_details
        )
        db.session.add(order)
        db.session.flush()

        for product, quantity, price, making_cost, _ in order_items_data:
            item = OrderItem(order_id=order.id, product_id=product.id, quantity=quantity, price=price, making_cost=making_cost)
            db.session.add(item)

        for p_addon in selected_addons:
            order_addon = AddOn(order_id=order.id, name=p_addon.name, price=p_addon.price)
            db.session.add(order_addon)

        db.session.commit()
        flash('Manual order created.', 'success')
        return redirect(url_for('admin_orders'))

    order_number = request.args.get('order_number', '').strip()
    customer_name = request.args.get('customer_name', '').strip()
    status_filter = request.args.get('status', '').strip()
    date_filter = request.args.get('date', 'today').strip().lower()

    query = Order.query.order_by(Order.created_at.desc())
    if order_number:
        query = query.filter(Order.order_number.ilike(f"%{order_number}%"))
    if customer_name:
        query = query.join(User).filter(User.name.ilike(f"%{customer_name}%") | User.email.ilike(f"%{customer_name}%"))

    # Date filters
    today = datetime.utcnow().date()
    if date_filter == 'today':
        start = datetime(today.year, today.month, today.day)
        end = start + timedelta(days=1)
        query = query.filter(Order.created_at >= start, Order.created_at < end)
    elif date_filter == 'yesterday' or date_filter == 'last':
        yesterday_date = today - timedelta(days=1)
        start = datetime(yesterday_date.year, yesterday_date.month, yesterday_date.day)
        end = start + timedelta(days=1)
        query = query.filter(Order.created_at >= start, Order.created_at < end)
    elif date_filter != 'all' and date_filter:
        # specific date prompt in YYYY-MM-DD
        try:
            specific = datetime.strptime(date_filter, '%Y-%m-%d').date()
            start = datetime(specific.year, specific.month, specific.day)
            end = start + timedelta(days=1)
            query = query.filter(Order.created_at >= start, Order.created_at < end)
        except ValueError:
            pass

    # Status filters
    if status_filter:
        if status_filter.lower() == 'pending':
            query = query.filter(Order.status.in_(['Pending', 'Pending Payment', 'Under Process']))
        elif status_filter.lower() == 'complete':
            query = query.filter(Order.status == 'Complete')
        else:
            query = query.filter(Order.status.ilike(f"%{status_filter}%"))

    orders = query.all()
    users = User.query.all()
    products = Product.query.all()
    return render_template(
        'admin/orders.html',
        orders=orders,
        users=users,
        products=products,
        order_number=order_number,
        customer_name=customer_name,
        status_filter=status_filter,
        date_filter=date_filter
    )


@app.route('/admin/orders/<int:order_id>/status', methods=['POST'])
@login_required
@admin_required
def update_order_status(order_id):
    order = Order.query.get_or_404(order_id)
    order.status = request.form.get('status')
    db.session.commit()
    flash('Order status updated.', 'success')
    return redirect(url_for('admin_orders'))

@app.route('/admin/orders/<int:order_id>/record_payment', methods=['POST'])
@login_required
@admin_required
def record_order_payment(order_id):
    order = Order.query.get_or_404(order_id)
    paid_amount = float(request.form.get('paid_amount', 0) or 0)
    if paid_amount < 0:
        flash('Paid amount cannot be negative.', 'danger')
        return redirect(url_for('admin_orders'))

    order.amount_paid += paid_amount
    if order.amount_paid >= order.total_price:
        order.amount_paid = order.total_price
        order.status = 'Complete'
    elif order.amount_paid > 0 and order.status == 'Pending Payment':
        order.status = 'Under Process'

    db.session.commit()

    # Update warning status on user due threshold after recording payment
    ensure_user_due_warning(order.user)

    flash('Payment recorded for order and warning status updated.', 'success')
    return redirect(url_for('admin_orders'))

@app.route('/admin/orders/<int:order_id>/confirm_payment', methods=['POST'])
@login_required
@admin_required
def confirm_order_payment(order_id):
    order = Order.query.get_or_404(order_id)
    if order.status == 'Pending Payment':
        order.status = 'Under Process'
        db.session.commit()
        flash('Payment confirmed and order moved to Under Process.', 'success')
    else:
        flash('Order is not pending payment.', 'warning')
    return redirect(url_for('admin_orders'))

@app.route('/admin/orders/<int:order_id>/addons', methods=['POST'])
@login_required
@admin_required
def add_addon(order_id):
    order = Order.query.get_or_404(order_id)
    name = request.form['name']
    price = float(request.form['price'])
    addon = AddOn(order_id=order.id, name=name, price=price)
    db.session.add(addon)
    order.total_price += price
    db.session.commit()
    flash('Add-on added to order.', 'success')
    return redirect(url_for('admin_orders'))

@app.route('/admin/custom_orders', methods=['GET'])
@login_required
@admin_required
def admin_custom_orders():
    order_number = request.args.get('order_number', '').strip()
    customer_name = request.args.get('customer_name', '').strip()
    status_filter = request.args.get('status', '').strip()
    date_filter = request.args.get('date', 'all').strip().lower()

    query = CustomOrder.query.order_by(CustomOrder.created_at.desc())

    if order_number:
        query = query.filter(CustomOrder.custom_order_number.ilike(f"%{order_number}%") | CustomOrder.title.ilike(f"%{order_number}%"))

    if customer_name:
        query = query.outerjoin(User).filter(
            db.or_(
                User.name.ilike(f"%{customer_name}%"),
                User.email.ilike(f"%{customer_name}%"),
                CustomOrder.customer_name.ilike(f"%{customer_name}%"),
                CustomOrder.customer_email.ilike(f"%{customer_name}%")
            )
        )

    # date filters
    today = datetime.utcnow().date()
    if date_filter == 'today':
        start = datetime(today.year, today.month, today.day)
        end = start + timedelta(days=1)
        query = query.filter(CustomOrder.created_at >= start, CustomOrder.created_at < end)
    elif date_filter in ['yesterday', 'last']:
        yesterday_date = today - timedelta(days=1)
        start = datetime(yesterday_date.year, yesterday_date.month, yesterday_date.day)
        end = start + timedelta(days=1)
        query = query.filter(CustomOrder.created_at >= start, CustomOrder.created_at < end)
    elif date_filter and date_filter != 'all':
        try:
            specific = datetime.strptime(date_filter, '%Y-%m-%d').date()
            start = datetime(specific.year, specific.month, specific.day)
            end = start + timedelta(days=1)
            query = query.filter(CustomOrder.created_at >= start, CustomOrder.created_at < end)
        except ValueError:
            pass

    if status_filter:
        if status_filter.lower() == 'pending':
            query = query.filter(CustomOrder.status == 'Pending')
        elif status_filter.lower() == 'complete':
            query = query.filter(CustomOrder.status == 'Complete')
        else:
            query = query.filter(CustomOrder.status.ilike(f"%{status_filter}%"))

    requests = query.all()
    users = User.query.order_by(User.name).all()
    return render_template(
        'admin/custom_orders.html',
        requests=requests,
        users=users,
        order_number=order_number,
        customer_name=customer_name,
        status_filter=status_filter,
        date_filter=date_filter
    )

@app.route('/admin/custom_orders/<int:custom_id>', methods=['GET'])
@login_required
@admin_required
def admin_custom_order_view(custom_id):
    custom = CustomOrder.query.get_or_404(custom_id)
    return render_template('admin/custom_order_view.html', custom=custom)

@app.route('/admin/custom_orders/create', methods=['POST'])
@login_required
@admin_required
def admin_create_custom_order():
    user_id = int(request.form.get('user_id'))
    title = request.form.get('title')
    description = request.form.get('description')
    quantity = int(request.form.get('quantity', 1))
    uploaded_image = request.form.get('image_url') or None
    customer_name = request.form.get('customer_name')
    customer_email = request.form.get('customer_email')

    if not title or not description or not customer_name or not customer_email:
        flash('Title, description, customer name and customer email are required.', 'danger')
        return redirect(url_for('admin_custom_orders'))

    custom = CustomOrder(
        custom_order_number=f"CO{generate_order_number()}",
        user_id=user_id,
        customer_name=customer_name,
        customer_email=customer_email,
        title=title,
        description=description,
        quantity=quantity,
        uploaded_image=uploaded_image,
        status='Pending'
    )
    db.session.add(custom)
    db.session.commit()
    flash('Custom order created successfully.', 'success')
    return redirect(url_for('admin_custom_orders'))

@app.route('/admin/custom_orders/<int:custom_id>/status', methods=['POST'])
@login_required
@admin_required
def custom_order_status(custom_id):
    custom = CustomOrder.query.get_or_404(custom_id)
    status = request.form.get('status')
    if status in ['Pending', 'Quoted', 'Confirmed', 'Under Process', 'Shipped', 'Complete']:
        custom.status = status
        db.session.commit()
        flash('Custom order status updated.', 'success')
    else:
        flash('Invalid status value.', 'danger')
    return redirect(url_for('admin_custom_orders'))

@app.route('/admin/custom_orders/<int:custom_id>/convert', methods=['POST'])
@login_required
@admin_required
def convert_custom_order(custom_id):
    custom = CustomOrder.query.get_or_404(custom_id)
    if custom.estimate_price is None:
        flash('Cannot convert without estimate.', 'danger')
        return redirect(url_for('admin_custom_orders'))

    if custom.status not in ['Confirmed', 'Awaiting Conversion']:
        flash('Custom order must be confirmed/awaiting conversion first.', 'warning')
        return redirect(url_for('admin_custom_orders'))

    total_price = custom.estimate_price * custom.quantity
    order = Order(user_id=custom.user_id, total_price=total_price, status='Under Process', order_number=generate_order_number(), order_name=custom.title)
    db.session.add(order)
    db.session.flush()
    item = OrderItem(order_id=order.id, product_id=0, quantity=custom.quantity, price=custom.estimate_price)
    # product_id=0 for custom product placeholder
    db.session.add(item)
    custom.status = 'Converted'
    db.session.commit()
    flash('Custom order converted to normal order.', 'success')
    return redirect(url_for('admin_custom_orders'))

@app.route('/admin/custom_orders/<int:custom_id>/estimate', methods=['POST'])
@login_required
@admin_required
def estimate_custom_order(custom_id):
    custom = CustomOrder.query.get_or_404(custom_id)
    custom.estimate_price = float(request.form['estimate_price'])
    custom.status = 'Quoted'
    db.session.commit()
    flash('Estimate submitted.', 'success')
    return redirect(url_for('admin_custom_orders'))

@app.route('/admin/custom_orders/<int:custom_id>/delete', methods=['POST'])
@login_required
@admin_required
def admin_delete_custom_order(custom_id):
    custom = CustomOrder.query.get_or_404(custom_id)
    db.session.delete(custom)
    db.session.commit()
    flash('Custom order deleted successfully.', 'success')
    return redirect(url_for('admin_custom_orders'))

@app.route('/google5df79dc8ebd34cf3.html')
def google_verification():
    return send_from_directory(BASE_DIR, 'google5df79dc8ebd34cf3.html')

@app.route('/admin/custom_orders/<int:custom_id>/view')
@login_required
@admin_required
def admin_view_custom_order(custom_id):
    custom = CustomOrder.query.get_or_404(custom_id)
    return render_template('admin/custom_order_details.html', custom=custom)

@app.route('/sitemap.xml', methods=['GET'])
def sitemap():
    pages = set()

    # include all public GET routes without required URL params
    for rule in app.url_map.iter_rules():
        if 'GET' in rule.methods and len(rule.arguments) == 0:
            if rule.endpoint in ('static',):
                continue
            if rule.rule.startswith('/admin'):
                continue
            pages.add(url_for(rule.endpoint, _external=True))

    # include additional internal/custom routes
    pages.update({
        url_for('login', _external=True),
        url_for('signup', _external=True),
        url_for('profile', _external=True),
        url_for('order_history', _external=True),
        url_for('custom_orders', _external=True),
        url_for('google_verification', _external=True),
    })

    # include product detail pages
    products = Product.query.order_by(Product.id).all()
    for product in products:
        pages.add(url_for('product_detail', product_id=product.id, _external=True))

    # include custom order list only if endpoint exists
    pages.add(url_for('custom_orders', _external=True))

    sitemap_xml = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for page in sorted(pages):
        sitemap_xml.append('  <url>')
        sitemap_xml.append(f'    <loc>{page}</loc>')
        sitemap_xml.append('    <changefreq>weekly</changefreq>')
        sitemap_xml.append('    <priority>0.8</priority>')
        sitemap_xml.append('  </url>')
    sitemap_xml.append('</urlset>')

    return app.response_class('\n'.join(sitemap_xml), mimetype='application/xml')

@app.route('/graph/<graph_type>')
@login_required
@admin_required
def graph(graph_type):
    fig = Figure(figsize=(6, 4))
    ax = fig.subplots()
    if graph_type == 'sales':
        data = db.session.query(Order.created_at, db.func.sum(Order.total_price)).group_by(db.func.date(Order.created_at)).all()
        x = [d.strftime('%Y-%m-%d') for d,_ in data]
        y = [float(v) for _,v in data]
        ax.plot(x, y, marker='o')
        ax.set_title('Sales per Day')
        ax.set_xlabel('Date')
        ax.set_ylabel('Total Sales (PKR)')
    elif graph_type == 'orders':
        data = db.session.query(db.func.date(Order.created_at), db.func.count(Order.id)).group_by(db.func.date(Order.created_at)).all()
        x = [r for r,_ in data]
        y = [int(c) for _,c in data]
        ax.bar(x, y)
        ax.set_title('Orders per Day')
    elif graph_type == 'top_products':
        data = db.session.query(Product.name, db.func.sum(OrderItem.quantity)).join(OrderItem, Product.id == OrderItem.product_id).group_by(Product.id).order_by(db.func.sum(OrderItem.quantity).desc()).limit(10).all()
        x = [name for name, _ in data]
        y = [int(qty) for _, qty in data]
        ax.barh(x, y)
        ax.set_title('Top Products')
    ax.tick_params(axis='x', rotation=45)
    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format='png')
    buf.seek(0)
    return send_file(buf, mimetype='image/png')

if __name__ == '__main__':
    init_db()
    app.run(debug=True)
