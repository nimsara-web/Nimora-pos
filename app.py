import os
import secrets
import json
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash
from pymongo import MongoClient
from bson.objectid import ObjectId
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev')

# ===== MongoDB connection =====
MONGO_URI = os.environ.get('MONGO_URI')
if not MONGO_URI:
    raise RuntimeError('MONGO_URI environment variable එක set කරන්න')

client = MongoClient(MONGO_URI)
db = client.get_database()          # URL එකේ තියෙන db name එක ගන්නවා (nimora_pos)
items_col = db.items
invoices_col = db.invoices


# ===== Routes =====

@app.route('/')
def index():
    items = list(items_col.find())
    return render_template('index.html', items=items)


@app.route('/items', methods=['GET', 'POST'])
def manage_items():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        try:
            price = float(request.form.get('price', 0))
            stock = int(request.form.get('stock', 0))
        except ValueError:
            flash('Price සහ Stock ඉලක්කම් වලින් දාන්න')
            return redirect(url_for('manage_items'))

        if name and price > 0:
            items_col.insert_one({
                'name': name,
                'price': price,
                'stock': stock,
                'created_at': datetime.utcnow()
            })
            flash('Item එක add කළා')
        else:
            flash('Name එකයි price එකයි හරියට දාන්න')
        return redirect(url_for('manage_items'))

    items = list(items_col.find())
    return render_template('items.html', items=items)


@app.route('/item/delete/<item_id>')
def delete_item(item_id):
    items_col.delete_one({'_id': ObjectId(item_id)})
    flash('Item එක delete කළා')
    return redirect(url_for('manage_items'))


@app.route('/item/edit/<item_id>', methods=['GET', 'POST'])
def edit_item(item_id):
    item = items_col.find_one({'_id': ObjectId(item_id)})
    if not item:
        flash('Item එක හම්බුනේ නෑ')
        return redirect(url_for('manage_items'))

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        try:
            price = float(request.form.get('price', 0))
            stock = int(request.form.get('stock', 0))
        except ValueError:
            flash('Price සහ Stock ඉලක්කම් වලින් දාන්න')
            return redirect(url_for('edit_item', item_id=item_id))

        items_col.update_one(
            {'_id': ObjectId(item_id)},
            {'$set': {'name': name, 'price': price, 'stock': stock}}
        )
        flash('Item එක update කළා')
        return redirect(url_for('manage_items'))

    return render_template('items.html', items=list(items_col.find()), edit_item=item)


@app.route('/invoice/create', methods=['POST'])
def create_invoice():
    selected = request.form.getlist('item_ids')
    if not selected:
        flash('Item එකක්වත් select කරන්න')
        return redirect(url_for('index'))

    ids = [ObjectId(i) for i in selected]
    items = list(items_col.find({'_id': {'$in': ids}}))
    if not items:
        flash('Select කරපු items හම්බුනේ නෑ')
        return redirect(url_for('index'))

    total = sum(i['price'] for i in items)
    invoice_no = f"NIM-{secrets.token_hex(4).upper()}"

    invoice_doc = {
        'invoice_no': invoice_no,
        'created_at': datetime.utcnow(),
        'total': total,
        'items': [{'name': i['name'], 'price': i['price']} for i in items]
    }
    result = invoices_col.insert_one(invoice_doc)

    return redirect(url_for('view_invoice', inv_id=str(result.inserted_id)))


@app.route('/invoice/<inv_id>')
def view_invoice(inv_id):
    inv = invoices_col.find_one({'_id': ObjectId(inv_id)})
    if not inv:
        flash('Invoice එක හම්බුනේ නෑ')
        return redirect(url_for('index'))

    return render_template(
        'invoice.html',
        inv=inv,
        items=inv['items'],
        logo=os.environ.get('LOGO_URL', ''),
        phone=os.environ.get('BUSINESS_PHONE', '0784280074'),
        business=os.environ.get('BUSINESS_NAME', 'Nimora Brand')
    )


@app.route('/invoices')
def invoices_list():
    invs = list(invoices_col.find().sort('created_at', -1))
    return render_template('invoices_list.html', invoices=invs)


if __name__ == '__main__':
    app.run(debug=True)
