import os
import secrets
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash
from pymongo import MongoClient
from bson.objectid import ObjectId
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev')

MONGO_URI = os.environ.get('MONGO_URI')
if not MONGO_URI:
    raise RuntimeError('MONGO_URI environment variable is not set')

client = MongoClient(MONGO_URI)
db = client.get_database('nimora_pos')
items_col = db.items
invoices_col = db.invoices
settings_col = db.settings


def get_settings():
    s = settings_col.find_one({'_id': 'main'})
    if not s:
        s = {
            '_id': 'main',
            'business_name': os.environ.get('BUSINESS_NAME', 'Nimora Brand'),
            'phone': os.environ.get('BUSINESS_PHONE', '0784280074'),
            'logo_url': os.environ.get('LOGO_URL', ''),
            'address': '',
            'tax_rate': 0,
            'currency': 'Rs.'
        }
        settings_col.insert_one(s)
    return s


@app.route('/')
def dashboard():
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_invoices = list(invoices_col.find({'created_at': {'$gte': today}}))
    today_sales = sum(i['total'] for i in today_invoices)
    total_items = items_col.count_documents({})
    low_stock = list(items_col.find({'stock': {'$lte': 5}}).limit(5))
    recent = list(invoices_col.find().sort('created_at', -1).limit(5))
    return render_template('dashboard.html',
                           today_sales=today_sales,
                           today_count=len(today_invoices),
                           total_items=total_items,
                           low_stock=low_stock,
                           recent=recent)


@app.route('/pos')
def index():
    q = request.args.get('q', '').strip()
    if q:
        items = list(items_col.find({'name': {'$regex': q, '$options': 'i'}}))
    else:
        items = list(items_col.find())
    settings = get_settings()
    return render_template('index.html', items=items, q=q, settings=settings)


@app.route('/invoice/create', methods=['POST'])
def create_invoice():
    selected = request.form.getlist('item_ids')
    if not selected:
        flash('Please select at least one item')
        return redirect(url_for('index'))

    ids = [ObjectId(i) for i in selected]
    items = list(items_col.find({'_id': {'$in': ids}}))
    if not items:
        flash('Selected items could not be found')
        return redirect(url_for('index'))

    subtotal = sum(i['price'] for i in items)
    discount = float(request.form.get('discount', 0) or 0)
    tax_rate = float(request.form.get('tax_rate', 0) or 0)
    customer = request.form.get('customer', '').strip()
    payment = request.form.get('payment', 'Cash')

    tax_amount = (subtotal - discount) * tax_rate / 100
    total = subtotal - discount + tax_amount
    invoice_no = f"NIM-{secrets.token_hex(3).upper()}"

    invoice_doc = {
        'invoice_no': invoice_no,
        'created_at': datetime.utcnow(),
        'customer': customer,
        'payment': payment,
        'subtotal': subtotal,
        'discount': discount,
        'tax_rate': tax_rate,
        'tax_amount': tax_amount,
        'total': total,
        'items': [{'name': i['name'], 'price': i['price']} for i in items]
    }
    result = invoices_col.insert_one(invoice_doc)

    for i in items:
        items_col.update_one({'_id': i['_id']}, {'$inc': {'stock': -1}})

    return redirect(url_for('view_invoice', inv_id=str(result.inserted_id)))


@app.route('/invoice/<inv_id>')
def view_invoice(inv_id):
    inv = invoices_col.find_one({'_id': ObjectId(inv_id)})
    if not inv:
        flash('Invoice not found')
        return redirect(url_for('dashboard'))
    settings = get_settings()
    return render_template('invoice.html', inv=inv, items=inv['items'], settings=settings)


@app.route('/invoices')
def invoices_list():
    q = request.args.get('q', '').strip()
    query = {}
    if q:
        query = {'$or': [
            {'invoice_no': {'$regex': q, '$options': 'i'}},
            {'customer': {'$regex': q, '$options': 'i'}}
        ]}
    invs = list(invoices_col.find(query).sort('created_at', -1))
    return render_template('invoices_list.html', invoices=invs, q=q)


@app.route('/items', methods=['GET', 'POST'])
def manage_items():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        try:
            price = float(request.form.get('price', 0))
            stock = int(request.form.get('stock', 0))
        except ValueError:
            flash('Price and Stock must be numbers')
            return redirect(url_for('manage_items'))

        if name and price > 0:
            items_col.insert_one({
                'name': name, 'price': price, 'stock': stock,
                'created_at': datetime.utcnow()
            })
            flash('Item added successfully')
        else:
            flash('Please enter a valid name and price')
        return redirect(url_for('manage_items'))

    items = list(items_col.find().sort('name', 1))
    return render_template('items.html', items=items)


@app.route('/item/delete/<item_id>')
def delete_item(item_id):
    items_col.delete_one({'_id': ObjectId(item_id)})
    flash('Item deleted')
    return redirect(url_for('manage_items'))


@app.route('/item/edit/<item_id>', methods=['GET', 'POST'])
def edit_item(item_id):
    item = items_col.find_one({'_id': ObjectId(item_id)})
    if not item:
        flash('Item not found')
        return redirect(url_for('manage_items'))

    if request.method == 'POST':
        try:
            price = float(request.form.get('price', 0))
            stock = int(request.form.get('stock', 0))
        except ValueError:
            flash('Price and Stock must be numbers')
            return redirect(url_for('edit_item', item_id=item_id))

        items_col.update_one(
            {'_id': ObjectId(item_id)},
            {'$set': {
                'name': request.form.get('name', '').strip(),
                'price': price, 'stock': stock
            }}
        )
        flash('Item updated')
        return redirect(url_for('manage_items'))

    return render_template('items.html',
                           items=list(items_col.find().sort('name', 1)),
                           edit_item=item)


if __name__ == '__main__':
    app.run(debug=True)
