import os
import io
import secrets
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, send_file, make_response
from pymongo import MongoClient
from bson.objectid import ObjectId
from dotenv import load_dotenv
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

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
suppliers_col = db.suppliers
returns_col = db.returns


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
            'currency': 'Rs.',
            'footer_note': 'Thank you for your business!',
            'low_stock_threshold': 5
        }
        settings_col.insert_one(s)
    return s


# ================= DASHBOARD =================
@app.route('/')
def dashboard():
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_invoices = list(invoices_col.find({'created_at': {'$gte': today}}))
    today_sales = sum(i['total'] for i in today_invoices)
    total_items = items_col.count_documents({})
    settings = get_settings()
    low_stock = list(items_col.find({'stock': {'$lte': settings['low_stock_threshold']}}).limit(5))
    recent = list(invoices_col.find().sort('created_at', -1).limit(5))
    week_ago = today - timedelta(days=7)
    week_invoices = list(invoices_col.find({'created_at': {'$gte': week_ago}}))
    week_sales = sum(i['total'] for i in week_invoices)
    return render_template('dashboard.html',
                           today_sales=today_sales,
                           today_count=len(today_invoices),
                           total_items=total_items,
                           low_stock=low_stock,
                           recent=recent,
                           week_sales=week_sales,
                           week_count=len(week_invoices))


# ================= POS / BILLING =================
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
    notes = request.form.get('notes', '').strip()

    tax_amount = (subtotal - discount) * tax_rate / 100
    total = subtotal - discount + tax_amount
    invoice_no = f"NIM-{secrets.token_hex(3).upper()}"

    invoice_doc = {
        'invoice_no': invoice_no,
        'created_at': datetime.utcnow(),
        'customer': customer,
        'payment': payment,
        'notes': notes,
        'subtotal': subtotal,
        'discount': discount,
        'tax_rate': tax_rate,
        'tax_amount': tax_amount,
        'total': total,
        'items': [{'item_id': str(i['_id']), 'name': i['name'], 'price': i['price']} for i in items]
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


@app.route('/invoice/<inv_id>/pdf')
def invoice_pdf(inv_id):
    inv = invoices_col.find_one({'_id': ObjectId(inv_id)})
    if not inv:
        flash('Invoice not found')
        return redirect(url_for('dashboard'))
    settings = get_settings()

    buf = io.BytesIO()
    p = canvas.Canvas(buf, pagesize=A4)
    width, height = A4

    p.setFont('Helvetica-Bold', 18)
    p.drawString(50, height - 50, settings['business_name'])
    p.setFont('Helvetica', 10)
    p.drawString(50, height - 70, f"Tel: {settings['phone']}")
    if settings.get('address'):
        p.drawString(50, height - 85, settings['address'])

    p.setFont('Helvetica-Bold', 12)
    p.drawString(50, height - 120, f"Invoice: {inv['invoice_no']}")
    p.drawString(50, height - 135, f"Date: {inv['created_at'].strftime('%Y-%m-%d %H:%M')}")
    if inv.get('customer'):
        p.drawString(50, height - 150, f"Customer: {inv['customer']}")
    p.drawString(50, height - 165, f"Payment: {inv['payment']}")

    y = height - 200
    p.setFont('Helvetica-Bold', 11)
    p.drawString(50, y, "Item")
    p.drawString(400, y, "Price")
    y -= 15
    p.setFont('Helvetica', 10)
    for it in inv['items']:
        p.drawString(50, y, it['name'][:60])
        p.drawString(400, y, f"Rs. {it['price']:.2f}")
        y -= 15

    y -= 10
    p.line(50, y, width - 50, y)
    y -= 20
    p.drawString(350, y, f"Subtotal: Rs. {inv['subtotal']:.2f}")
    y -= 15
    if inv.get('discount'):
        p.drawString(350, y, f"Discount: -Rs. {inv['discount']:.2f}")
        y -= 15
    if inv.get('tax_amount'):
        p.drawString(350, y, f"Tax ({inv['tax_rate']}%): Rs. {inv['tax_amount']:.2f}")
        y -= 15
    p.setFont('Helvetica-Bold', 12)
    p.drawString(350, y, f"Total: Rs. {inv['total']:.2f}")

    p.setFont('Helvetica-Oblique', 9)
    p.drawString(50, 50, settings.get('footer_note', 'Thank you!'))

    p.showPage()
    p.save()
    buf.seek(0)
    return send_file(buf, mimetype='application/pdf',
                     download_name=f"{inv['invoice_no']}.pdf", as_attachment=True)


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


# ================= ITEMS =================
@app.route('/items', methods=['GET', 'POST'])
def manage_items():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        try:
            price = float(request.form.get('price', 0))
            stock = int(request.form.get('stock', 0))
            cost = float(request.form.get('cost', 0) or 0)
        except ValueError:
            flash('Price, Stock and Cost must be numbers')
            return redirect(url_for('manage_items'))

        if name and price > 0:
            items_col.insert_one({
                'name': name, 'price': price, 'stock': stock, 'cost': cost,
                'barcode': request.form.get('barcode', '').strip(),
                'supplier': request.form.get('supplier', '').strip(),
                'created_at': datetime.utcnow()
            })
            flash('Item added successfully')
        else:
            flash('Please enter a valid name and price')
        return redirect(url_for('manage_items'))

    items = list(items_col.find().sort('name', 1))
    suppliers = list(suppliers_col.find())
    return render_template('items.html', items=items, suppliers=suppliers)


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
            cost = float(request.form.get('cost', 0) or 0)
        except ValueError:
            flash('Price, Stock and Cost must be numbers')
            return redirect(url_for('edit_item', item_id=item_id))

        items_col.update_one(
            {'_id': ObjectId(item_id)},
            {'$set': {
                'name': request.form.get('name', '').strip(),
                'price': price, 'stock': stock, 'cost': cost,
                'barcode': request.form.get('barcode', '').strip(),
                'supplier': request.form.get('supplier', '').strip()
            }}
        )
        flash('Item updated')
        return redirect(url_for('manage_items'))

    return render_template('items.html',
                           items=list(items_col.find().sort('name', 1)),
                           suppliers=list(suppliers_col.find()),
                           edit_item=item)


# ================= SUPPLIERS =================
@app.route('/suppliers', methods=['GET', 'POST'])
def suppliers():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        if name:
            suppliers_col.insert_one({
                'name': name,
                'phone': request.form.get('phone', '').strip(),
                'email': request.form.get('email', '').strip(),
                'address': request.form.get('address', '').strip(),
                'created_at': datetime.utcnow()
            })
            flash('Supplier added')
        return redirect(url_for('suppliers'))
    sup = list(suppliers_col.find().sort('name', 1))
    return render_template('suppliers.html', suppliers=sup)


@app.route('/supplier/delete/<sid>')
def delete_supplier(sid):
    suppliers_col.delete_one({'_id': ObjectId(sid)})
    flash('Supplier deleted')
    return redirect(url_for('suppliers'))


# ================= RETURNS =================
@app.route('/returns', methods=['GET', 'POST'])
def returns():
    if request.method == 'POST':
        inv_no = request.form.get('invoice_no', '').strip()
        inv = invoices_col.find_one({'invoice_no': inv_no})
        if not inv:
            flash('Invoice not found')
            return redirect(url_for('returns'))

        return_doc = {
            'invoice_no': inv_no,
            'created_at': datetime.utcnow(),
            'reason': request.form.get('reason', '').strip(),
            'amount': inv['total'],
            'items': inv['items']
        }
        returns_col.insert_one(return_doc)
        for it in inv['items']:
            if it.get('item_id'):
                items_col.update_one({'_id': ObjectId(it['item_id'])}, {'$inc': {'stock': 1}})
        flash('Return recorded and stock restored')
        return redirect(url_for('returns'))

    rets = list(returns_col.find().sort('created_at', -1))
    return render_template('returns.html', returns=rets)


# ================= REPORTS =================
@app.route('/reports')
def reports():
    start = request.args.get('start')
    end = request.args.get('end')
    query = {}
    if start or end:
        query['created_at'] = {}
        if start:
            query['created_at']['$gte'] = datetime.strptime(start, '%Y-%m-%d')
        if end:
            query['created_at']['$lte'] = datetime.strptime(end, '%Y-%m-%d').replace(hour=23, minute=59, second=59)

    invs = list(invoices_col.find(query).sort('created_at', -1))
    total = sum(i['total'] for i in invs)
    count = len(invs)

    # item sales summary
    item_summary = {}
    for inv in invs:
        for it in inv['items']:
            key = it['name']
            if key not in item_summary:
                item_summary[key] = {'qty': 0, 'total': 0}
            item_summary[key]['qty'] += 1
            item_summary[key]['total'] += it['price']

    summary_list = sorted(item_summary.items(), key=lambda x: x[1]['total'], reverse=True)
    return render_template('reports.html', invoices=invs, total=total, count=count,
                           item_summary=summary_list, start=start, end=end)


# ================= SETTINGS =================
@app.route('/settings', methods=['GET', 'POST'])
def settings():
    s = get_settings()
    if request.method == 'POST':
        try:
            tax_rate = float(request.form.get('tax_rate', 0) or 0)
            low_stock = int(request.form.get('low_stock_threshold', 5) or 5)
        except ValueError:
            flash('Tax and threshold must be numbers')
            return redirect(url_for('settings'))
        settings_col.update_one({'_id': 'main'}, {'$set': {
            'business_name': request.form.get('business_name', s['business_name']),
            'phone': request.form.get('phone', s['phone']),
            'logo_url': request.form.get('logo_url', s['logo_url']),
            'address': request.form.get('address', ''),
            'tax_rate': tax_rate,
            'currency': request.form.get('currency', 'Rs.'),
            'footer_note': request.form.get('footer_note', ''),
            'low_stock_threshold': low_stock
        }})
        flash('Settings saved')
        return redirect(url_for('settings'))
    return render_template('settings.html', settings=s)


if __name__ == '__main__':
    app.run(debug=True)
