import base64
import hashlib
import hmac
import html
import json
import math
import os
import re
import secrets
import shutil
import socket
import threading
import time
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlparse

# ================= SETTINGS YOU CAN CHANGE =================
OWNER_PORT = 8000        # your private owner site (only opens on this computer)
PUBLIC_PORT = 8080       # your public shop (the one customers use)
PUBLIC_HOST = "0.0.0.0"  # "0.0.0.0" lets phones on your Wi-Fi open the public shop.
                         # Use "127.0.0.1" to keep it on this computer only.
# ===========================================================

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
IMG_DIR = os.path.join(DATA, "images")
BACKUP_DIR = os.path.join(DATA, "backups")
CONFIG_FILE = os.path.join(DATA, "config.json")
PRODUCTS_FILE = os.path.join(DATA, "products.json")
ORDERS_FILE = os.path.join(DATA, "orders.json")
BACKUP_FILES = ("config.json", "products.json", "orders.json")
IMG_RE = r"[0-9a-f]{12}-[0-9a-f]{6}\.jpg"
MAX_BODY = 8 * 1024 * 1024
MAX_ORDERS = 2000
MAX_ITEMS = 500
DEFAULT_CATEGORIES = ["Watches", "Shoes", "Shades", "Gadgets", "Accessories", "Rings", "Shirts"]
LOCK = threading.Lock()
SESSIONS = {}
FAILS = []
ORDER_TIMES = {}
LAST_BACKUP = [0.0]

FONT_LINK = '<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@500;600;700&family=Jost:wght@300;400;500&display=swap" rel="stylesheet">'


def default_shop():
    return {
        "name": "Faruk",
        "whatsapp": "",
        "email": "lawalumarfauk86@gmail.com",
        "address": "Janyau ta Gabas, after the Police Station",
        "accepting": True,
        "categories": list(DEFAULT_CATEGORIES),
        "banner": "",
        "public_url": "",
    }


# ---------- saving, loading and backups ----------
def backup_now(force=False):
    if not force and time.time() - LAST_BACKUP[0] < 1800:
        return
    LAST_BACKUP[0] = time.time()
    try:
        folder = os.path.join(BACKUP_DIR, time.strftime("%Y-%m-%d_%H%M%S"))
        os.makedirs(folder, exist_ok=True)
        copied = 0
        for name in BACKUP_FILES:
            src = os.path.join(DATA, name)
            if os.path.exists(src):
                shutil.copy2(src, folder)
                copied += 1
        if not copied:
            shutil.rmtree(folder, ignore_errors=True)
        for old in sorted(os.listdir(BACKUP_DIR))[:-30]:
            shutil.rmtree(os.path.join(BACKUP_DIR, old), ignore_errors=True)
    except OSError:
        pass


def write_json(path, obj, backup=True):
    if backup and os.path.dirname(path) == DATA:
        backup_now()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def recover(path, default):
    # The file is damaged. Keep a copy of it, then use the newest good backup.
    try:
        shutil.copy2(path, "%s.damaged-%d" % (path, int(time.time())))
    except OSError:
        pass
    try:
        folders = sorted(os.listdir(BACKUP_DIR), reverse=True)
    except OSError:
        folders = []
    for folder in folders:
        try:
            with open(os.path.join(BACKUP_DIR, folder, os.path.basename(path)), "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        write_json(path, data, backup=False)
        return data
    write_json(path, default, backup=False)
    return default


def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        return recover(path, default)


def clean_categories(raw):
    if isinstance(raw, str):
        raw = raw.split(",")
    out = []
    for c in raw if isinstance(raw, list) else []:
        c = re.sub(r"\s+", " ", str(c)).strip()[:24]
        if c and c.lower() not in [x.lower() for x in out]:
            out.append(c)
    return out[:12] or list(DEFAULT_CATEGORIES)


def load_config():
    cfg = read_json(CONFIG_FILE, {})
    if not isinstance(cfg, dict):
        cfg = {}
    shop = default_shop()
    shop.update(cfg.get("shop", {}))
    if not shop.get("categories"):
        shop["categories"] = list(DEFAULT_CATEGORIES)
    cfg["shop"] = shop
    return cfg


def load_list(path):
    items = read_json(path, [])
    return items if isinstance(items, list) else []


def load_products():
    items = [p for p in load_list(PRODUCTS_FILE)
             if isinstance(p, dict) and "id" in p and "name" in p and "price" in p]
    for p in items:
        p.setdefault("category", "Watches")
        p.setdefault("stock", None)
        p.setdefault("available", True)
    return items


def load_orders():
    return [o for o in load_list(ORDERS_FILE) if isinstance(o, dict) and "id" in o]


def in_stock(p):
    stock = p.get("stock")
    return bool(p.get("available", True)) and (stock is None or stock > 0)


# ---------- password ----------
def hash_pw(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 200000).hex()


def check_password(password):
    cfg = read_json(CONFIG_FILE, {})
    if "salt" not in cfg or "hash" not in cfg:
        return False
    return hmac.compare_digest(hash_pw(password, cfg["salt"]), cfg["hash"])


def first_run_setup():
    cfg = read_json(CONFIG_FILE, {})
    if isinstance(cfg, dict) and "hash" in cfg:
        return
    print("First time setup. Choose a password for your owner login.")
    print("(You will see the letters as you type. That is normal here.)")
    while True:
        p1 = input("New password (at least 8 characters): ")
        if len(p1) < 8:
            print("Too short. Try again.")
            continue
        if p1 != input("Type it again: "):
            print("They did not match. Try again.")
            continue
        break
    salt = secrets.token_hex(16)
    cfg = cfg if isinstance(cfg, dict) else {}
    cfg.update({"salt": salt, "hash": hash_pw(p1, salt)})
    cfg.setdefault("shop", default_shop())
    write_json(CONFIG_FILE, cfg)
    print("Password saved.")


# ---------- items ----------
def remove_image(name):
    if name and re.fullmatch(IMG_RE, name):
        try:
            os.remove(os.path.join(IMG_DIR, name))
        except OSError:
            pass


def store_image(pid, data_url, old):
    m = re.fullmatch(r"data:image/jpeg;base64,([A-Za-z0-9+/=]+)", data_url)
    if not m:
        raise ValueError("Use a JPG or PNG photo.")
    try:
        raw = base64.b64decode(m.group(1))
    except ValueError:
        raise ValueError("That photo could not be read.")
    if raw[:3] != b"\xff\xd8\xff" or len(raw) > 5 * 1024 * 1024:
        raise ValueError("That photo could not be used.")
    os.makedirs(IMG_DIR, exist_ok=True)
    name = "%s-%s.jpg" % (pid, secrets.token_hex(3))
    with open(os.path.join(IMG_DIR, name), "wb") as f:
        f.write(raw)
    remove_image(old)
    return name


def save_product(d):
    name = str(d.get("name", "")).strip()[:80]
    if not name:
        raise ValueError("Enter the item name.")
    try:
        price = float(d.get("price"))
    except (TypeError, ValueError):
        raise ValueError("Enter a valid price.")
    if not math.isfinite(price) or price < 0 or price > 1e9:
        raise ValueError("Enter a valid price.")
    price = int(price) if price == int(price) else round(price, 2)
    desc = str(d.get("description", "")).strip()[:300]
    available = bool(d.get("available", True))
    category = re.sub(r"\s+", " ", str(d.get("category", ""))).strip()[:24] or load_config()["shop"]["categories"][0]
    stock = d.get("stock")
    if stock is None or str(stock).strip() == "":
        stock = None
    else:
        try:
            n = float(stock)
        except (TypeError, ValueError):
            raise ValueError("Stock must be a whole number, or leave it empty.")
        if not math.isfinite(n) or n != int(n) or n < 0 or n > 100000:
            raise ValueError("Stock must be a whole number, or leave it empty.")
        stock = int(n)
    pid = str(d.get("id", ""))
    image = d.get("image") or ""
    with LOCK:
        items = load_products()
        existing = next((p for p in items if p.get("id") == pid), None) if pid else None
        if pid and not existing:
            raise ValueError("That item was not found.")
        if not existing and len(items) >= MAX_ITEMS:
            raise ValueError("You have reached the limit of %d items." % MAX_ITEMS)
        p = existing or {"id": secrets.token_hex(6), "image": ""}
        p.update({"name": name, "price": price, "description": desc, "available": available,
                  "category": category, "stock": stock})
        if image:
            p["image"] = store_image(p["id"], image, p.get("image", ""))
        if not existing:
            items.insert(0, p)
        write_json(PRODUCTS_FILE, items)
    return p


def delete_product(pid):
    with LOCK:
        items = load_products()
        keep = [p for p in items if p.get("id") != str(pid)]
        for p in items:
            if p.get("id") == str(pid):
                remove_image(p.get("image", ""))
        write_json(PRODUCTS_FILE, keep)


def save_settings(d):
    email = str(d.get("email", "")).strip()[:80]
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError("That email does not look right.")
    url = str(d.get("public_url", "")).strip()[:200]
    if url and not re.fullmatch(r"https?://[^\s<>\"']+", url):
        raise ValueError("The internet address must start with https:// or http://")
    with LOCK:
        cfg = read_json(CONFIG_FILE, {})
        banner = load_config()["shop"].get("banner", "")
        if d.get("banner"):
            banner = store_image(secrets.token_hex(6), d["banner"], banner)
        elif d.get("clear_banner"):
            remove_image(banner)
            banner = ""
        cfg["shop"] = {
            "name": str(d.get("name", "")).strip()[:40] or "Faruk",
            "whatsapp": re.sub(r"[^0-9+ ]", "", str(d.get("whatsapp", "")))[:20],
            "email": email,
            "address": str(d.get("address", "")).strip()[:120],
            "accepting": bool(d.get("accepting", True)),
            "categories": clean_categories(d.get("categories", "")),
            "banner": banner,
            "public_url": url,
        }
        write_json(CONFIG_FILE, cfg)


# ---------- orders ----------
def create_order(d):
    if not load_config()["shop"].get("accepting", True):
        raise ValueError("Orders are paused right now. Please message us on WhatsApp.")
    name = str(d.get("name", "")).strip()[:60]
    if len(name) < 2:
        raise ValueError("Enter your name.")
    phone = re.sub(r"[^0-9+ ]", "", str(d.get("phone", "")))[:20].strip()
    if len(re.sub(r"\D", "", phone)) < 7:
        raise ValueError("Enter a phone number we can reach.")
    note = str(d.get("note", "")).strip()[:300]
    raw = d.get("items")
    if not isinstance(raw, list) or not raw or len(raw) > 20:
        raise ValueError("Your cart is empty.")
    with LOCK:
        products = {p["id"]: p for p in load_products()}
        wanted = {}
        for it in raw:
            if not isinstance(it, dict):
                raise ValueError("Something is wrong with your cart.")
            try:
                qty = int(it.get("qty"))
            except (TypeError, ValueError):
                raise ValueError("Something is wrong with your cart.")
            if qty < 1 or qty > 10:
                raise ValueError("You can order 1 to 10 of each item.")
            pid = str(it.get("id", ""))
            wanted[pid] = min(10, wanted.get(pid, 0) + qty)
        lines, total = [], 0
        for pid, qty in wanted.items():
            p = products.get(pid)
            if not p or not in_stock(p):
                raise ValueError("Sorry, an item in your cart is no longer available.")
            if p.get("stock") is not None and qty > p["stock"]:
                raise ValueError("Only %d of %s left." % (p["stock"], p["name"]))
            lines.append({"id": pid, "name": p["name"], "price": p["price"], "qty": qty})
            total += p["price"] * qty
        orders = load_orders()
        if len(orders) >= MAX_ORDERS:
            raise ValueError("The shop cannot take more orders right now.")
        oid = secrets.token_hex(4)
        while any(o.get("id") == oid for o in orders):
            oid = secrets.token_hex(4)
        order = {"id": oid, "created": time.strftime("%d %b %Y, %H:%M"), "name": name, "phone": phone,
                 "note": note, "items": lines, "total": round(total, 2), "status": "open"}
        orders.insert(0, order)
        write_json(ORDERS_FILE, orders)
        changed = False
        for pid, qty in wanted.items():
            if products[pid].get("stock") is not None:
                products[pid]["stock"] -= qty
                changed = True
        if changed:
            write_json(PRODUCTS_FILE, list(products.values()))
    return order


def set_order_status(oid, status):
    if status not in ("open", "closed"):
        raise ValueError("Bad status.")
    with LOCK:
        orders = load_orders()
        found = next((o for o in orders if o.get("id") == str(oid)), None)
        if not found:
            raise ValueError("Order not found.")
        found["status"] = status
        write_json(ORDERS_FILE, orders)


def delete_order(oid):
    with LOCK:
        write_json(ORDERS_FILE, [o for o in load_orders() if o.get("id") != str(oid)])


# ---------- public shop ----------
def e(text):
    return html.escape(str(text))


def money(n):
    n = float(n)
    return "\u20a6" + (format(int(n), ",") if n.is_integer() else format(n, ",.2f"))


def wa_digits(num):
    d = re.sub(r"\D", "", num or "")
    return "234" + d[1:] if d.startswith("0") else d


def fill(template, values):
    return re.sub(r"@([A-Z]+)@", lambda m: values[m.group(1)], template)


PUBLIC_CSS = """
[hidden]{display:none!important}
:root{--gold:#d4b46a;--ink:#eef3ff;--muted:#9fb3d6;--line:#1f3a6e;--card:#0d2147}
*{box-sizing:border-box}
:focus-visible{outline:2px solid var(--gold);outline-offset:3px}
body{margin:0;min-height:100vh;background:radial-gradient(1200px 640px at 50% -220px,#1a4690,#07142e 62%) #07142e;color:var(--ink);font:300 17px/1.7 Jost,"Segoe UI",system-ui,sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:1040px;margin:0 auto;padding:0 22px}
.hero{padding:84px 22px 40px;text-align:center;background-size:cover;background-position:center}
.shop{letter-spacing:.42em;text-transform:uppercase;color:var(--gold);font-size:13px;font-weight:400}
h1{font:600 clamp(48px,11vw,96px)/1 "Cormorant Garamond",Georgia,serif;margin:18px 0 0;letter-spacing:.02em}
.rule{width:80px;height:1px;background:linear-gradient(90deg,transparent,var(--gold),transparent);margin:26px auto 16px}
.tag{color:var(--muted);letter-spacing:.2em;text-transform:uppercase;font-size:12px}
.pause{color:#ff9f9f;margin:18px 0 0}
.filters{display:flex;gap:10px;flex-wrap:wrap;align-items:center;justify-content:center;padding-top:34px}
.filters input{width:min(260px,100%)}
.chip{background:transparent;border:1px solid #2b4a85;color:var(--muted);padding:9px 18px;font:500 12px Jost,sans-serif;letter-spacing:.14em;text-transform:uppercase;cursor:pointer}
.chip[aria-pressed="true"]{border-color:var(--gold);color:var(--gold)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:28px;padding:30px 0 70px}
.card{background:var(--card);border:1px solid var(--line);display:flex;flex-direction:column;transition:border-color .3s,transform .3s}
.card:hover{border-color:var(--gold);transform:translateY(-3px)}
.card img,.ph{width:100%;aspect-ratio:4/5;object-fit:cover;display:flex;align-items:center;justify-content:center;background:#12295a;color:#7388b0;outline:1px solid rgba(212,180,106,.4);outline-offset:-12px}
.b{padding:22px;display:flex;flex-direction:column;gap:8px;flex:1;text-align:center}
.cat{color:var(--gold);font-size:11px;letter-spacing:.22em;text-transform:uppercase}
h3{margin:0;font:600 27px/1.15 "Cormorant Garamond",Georgia,serif;letter-spacing:.02em}
.d{margin:0;color:var(--muted);font-size:15px}
.price{font:600 26px "Cormorant Garamond",Georgia,serif;color:var(--gold);margin:6px 0 4px}
.few{color:#ffcf8a;font-size:13px;letter-spacing:.08em;margin:0 0 6px}
.btn{display:block;text-align:center;background:linear-gradient(135deg,#ecd59a,#b18a3c);color:#1a1405;padding:14px 20px;font:500 13px Jost,sans-serif;letter-spacing:.16em;text-transform:uppercase;text-decoration:none;border:0;cursor:pointer;width:100%}
.btn:hover{filter:brightness(1.08)}
.btn:disabled{opacity:.5;cursor:wait}
.btn.ghost{background:transparent;color:var(--gold);border:1px solid var(--gold);width:auto;display:inline-block}
.neg{display:block;color:var(--gold);font-size:12px;letter-spacing:.14em;text-transform:uppercase;text-decoration:none;padding:12px 0 2px}
.neg:hover{text-decoration:underline}
.sold{color:var(--muted);font-size:13px;letter-spacing:.2em;text-transform:uppercase;padding:14px 0;margin-top:auto}
.empty{text-align:center;color:var(--muted);padding:70px 0;font:500 24px "Cormorant Garamond",Georgia,serif}
.contact{text-align:center;padding:54px 0 24px;border-top:1px solid var(--line)}
.contact h2{font:600 clamp(30px,6vw,46px)/1.1 "Cormorant Garamond",Georgia,serif;margin:0}
.contact p{color:var(--muted);margin:8px 0 18px}
.contact .btn{margin:6px}
footer{text-align:center;color:var(--muted);padding:30px 0 90px;font-size:13px;letter-spacing:.16em;text-transform:uppercase}
footer a{color:#8da6d6;text-decoration:none}
.cartbtn{position:fixed;right:18px;bottom:calc(18px + env(safe-area-inset-bottom,0px));z-index:5;width:auto;padding:15px 26px;box-shadow:0 6px 24px rgba(0,0,0,.6)}
dialog{background:#0d2147;color:var(--ink);border:1px solid var(--gold);padding:24px;width:min(520px,calc(100% - 24px));max-height:90vh}
dialog::backdrop{background:rgba(2,8,24,.78)}
dialog h2{font:600 32px/1.1 "Cormorant Garamond",Georgia,serif;margin:0 0 12px;color:#ecd59a}
label{display:block;font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);margin:14px 0 6px}
input,textarea{width:100%;padding:12px;border:1px solid #2b4a85;background:#07142e;color:inherit;font:inherit}
input:focus,textarea:focus{border-color:var(--gold);outline:none}
.line{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:11px 0;border-bottom:1px solid var(--line)}
.q{display:flex;align-items:center;gap:10px}
.q button{width:32px;height:32px;border:1px solid #2b4a85;background:transparent;color:var(--ink);cursor:pointer;font-size:18px;line-height:1}
.row{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}
.row .btn{flex:1;text-align:center}
.msg{color:#ff9f9f;font-size:14px;min-height:20px;margin:8px 0 0}
.hint{color:var(--muted);font-size:14px;margin:14px 0 0}
"""

PUBLIC_JS = r"""
var C=JSON.parse(document.getElementById("catalog").textContent),P={},cart={},cat="All";
C.products.forEach(function(p){P[p.id]=p});
try{cart=JSON.parse(localStorage.getItem("cart")||"{}")}catch(e){cart={}}
function money(n){return "\u20a6"+Number(n).toLocaleString()}
function el(t,p){var e=document.createElement(t);for(var k in (p||{})){if(k==="text")e.textContent=p[k];else if(k.slice(0,2)==="on")e[k]=p[k];else e.setAttribute(k,p[k]);}
 for(var i=2;i<arguments.length;i++){var c=arguments[i];if(c)e.appendChild(typeof c==="string"?document.createTextNode(c):c);}return e}
function clean(){for(var k in cart){if(!P[k]||!(cart[k]>0))delete cart[k]}}
function count(){var n=0;for(var k in cart)n+=cart[k];var b=document.getElementById("cartbtn");b.textContent="Cart ("+n+")";b.hidden=!C.shop.accepting}
function save(){clean();try{localStorage.setItem("cart",JSON.stringify(cart))}catch(e){}count()}
function cartText(){var t="Hello "+C.shop.name+", I am interested in:\n",tot=0;
 Object.keys(cart).forEach(function(id){var p=P[id];tot+=p.price*cart[id];t+=cart[id]+" x "+p.name+" ("+money(p.price)+")\n"});
 return t+"Total: "+money(tot)}
function link(label,href){return el("a",{class:"btn ghost",href:href,target:"_blank",rel:"noopener",text:label})}
function applyFilter(){var qe=document.getElementById("q"),q=qe?qe.value.trim().toLowerCase():"",n=0;
 document.querySelectorAll(".card").forEach(function(c){var ok=(cat==="All"||c.getAttribute("data-cat")===cat)&&(!q||c.getAttribute("data-name").indexOf(q)>-1);c.hidden=!ok;if(ok)n++});
 var none=document.getElementById("none");if(none)none.hidden=n>0||!document.querySelector(".card")}
function openCart(){clean();var d=el("dialog",{});document.body.appendChild(d);d.addEventListener("close",function(){d.remove()});
 d.appendChild(el("h2",{text:"Your cart"}));var ids=Object.keys(cart);
 if(!ids.length){d.appendChild(el("p",{text:"Your cart is empty. Add an item to begin."}));
  d.appendChild(el("div",{class:"row"},el("button",{class:"btn ghost",text:"Close",onclick:function(){d.close()}})));d.showModal();return}
 var total=0;
 ids.forEach(function(id){var p=P[id];total+=p.price*cart[id];
  d.appendChild(el("div",{class:"line"},el("span",{text:p.name+" \u00b7 "+money(p.price)}),
   el("div",{class:"q"},
    el("button",{"aria-label":"Remove one",text:"\u2212",onclick:function(){cart[id]--;save();d.close();openCart()}}),
    el("span",{text:String(cart[id])}),
    el("button",{"aria-label":"Add one",text:"+",onclick:function(){cart[id]=Math.min(p.max,cart[id]+1);save();d.close();openCart()}}))))});
 d.appendChild(el("div",{class:"line"},el("strong",{text:"Total"}),el("strong",{text:money(total)})));
 var nm=el("input",{autocomplete:"name",maxlength:"60"}),ph=el("input",{autocomplete:"tel",inputmode:"tel",maxlength:"20"}),nt=el("textarea",{rows:"2",maxlength:"300",placeholder:"Anything we should know?"}),msg=el("p",{class:"msg"});
 d.appendChild(el("label",{text:"Your name"}));d.appendChild(nm);
 d.appendChild(el("label",{text:"Your phone number"}));d.appendChild(ph);
 d.appendChild(el("label",{text:"Note (optional)"}));d.appendChild(nt);d.appendChild(msg);
 var go=el("button",{class:"btn",text:"Place order"});
 go.onclick=function(){go.disabled=true;msg.textContent="";
  fetch("/api/order",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({items:Object.keys(cart).map(function(id){return{id:id,qty:cart[id]}}),name:nm.value,phone:ph.value,note:nt.value})})
  .then(function(r){return r.json().catch(function(){return{}}).then(function(j){return{ok:r.ok,j:j}})})
  .then(function(x){go.disabled=false;if(!x.ok){msg.textContent=x.j.error||"Could not place the order.";return}cart={};save();thanks(d,x.j.order)})
  .catch(function(){go.disabled=false;msg.textContent="Could not reach the shop. Try again."})};
 d.appendChild(el("div",{class:"row"},go,el("button",{class:"btn ghost",text:"Keep browsing",onclick:function(){d.close()}})));
 if(C.shop.wa){d.appendChild(el("p",{class:"hint",text:"Want a better price? Talk to us before you order."}));
  d.appendChild(el("div",{class:"row"},link("Negotiate on WhatsApp","https://wa.me/"+C.shop.wa+"?text="+encodeURIComponent(cartText()))))}
 d.showModal()}
function thanks(d,o){while(d.firstChild)d.removeChild(d.firstChild);
 var t="Hello "+C.shop.name+", I placed order #"+o.id+":\n";o.items.forEach(function(i){t+=i.qty+" x "+i.name+" ("+money(i.price)+")\n"});t+="Total: "+money(o.total)+"\nName: "+o.name;
 d.appendChild(el("h2",{text:"Thank you, "+o.name}));
 d.appendChild(el("p",{text:"Your order #"+o.id+" was received. Total "+money(o.total)+". We will contact you soon."}));
 d.appendChild(el("p",{class:"hint",text:"Want to negotiate the price or confirm quickly? Reach us:"}));
 var row=el("div",{class:"row"});
 if(C.shop.wa)row.appendChild(link("WhatsApp","https://wa.me/"+C.shop.wa+"?text="+encodeURIComponent(t)));
 if(C.shop.email)row.appendChild(link("Email","mailto:"+C.shop.email+"?subject="+encodeURIComponent("Order #"+o.id)+"&body="+encodeURIComponent(t)));
 row.appendChild(el("button",{class:"btn",text:"Done",onclick:function(){d.close()}}));d.appendChild(row)}
document.addEventListener("click",function(ev){
 var ch=ev.target.closest(".chip");
 if(ch){cat=ch.getAttribute("data-cat");document.querySelectorAll(".chip").forEach(function(x){x.setAttribute("aria-pressed",String(x===ch))});applyFilter();return}
 var b=ev.target.closest("[data-add]");if(!b)return;var id=b.getAttribute("data-add");
 cart[id]=Math.min(P[id]?P[id].max:10,(cart[id]||0)+1);save();b.textContent="Added \u2713";setTimeout(function(){b.textContent="Add to cart"},900)});
var qbox=document.getElementById("q");if(qbox)qbox.addEventListener("input",applyFilter);
document.getElementById("cartbtn").addEventListener("click",openCart);
count();
"""

PUBLIC_TEMPLATE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>@TITLE@ | Shop</title>
@FONTS@<style>@CSS@</style></head><body>
<header class="hero"@HEROSTYLE@><div class="shop">@SHOP@</div><h1>The Collection</h1><div class="rule"></div><div class="tag">@TAGLINE@</div>@BANNER@</header>
<div class="wrap">@FILTERS@@BODY@<p class="empty" id="none" hidden>Nothing matches your search.</p>@CONTACT@
<footer>@ADDRESS@@EMAILLINE@</footer></div>
<button class="btn cartbtn" id="cartbtn" hidden>Cart (0)</button>
<script type="application/json" id="catalog">@CATALOG@</script>
<script>@JS@</script></body></html>"""


def public_page():
    shop = load_config()["shop"]
    wa = wa_digits(shop.get("whatsapp", ""))
    email = shop.get("email", "")
    accepting = shop.get("accepting", True)
    products = load_products()
    order = shop["categories"] + sorted({p["category"] for p in products} - set(shop["categories"]))
    used = [c for c in order if any(p["category"] == c for p in products)]
    cards = []
    for p in products:
        ok = in_stock(p)
        if p.get("image"):
            img = '<img src="/img/%s" alt="%s" loading="lazy">' % (e(p["image"]), e(p["name"]))
        else:
            img = '<div class="ph">No photo yet</div>'
        if not ok:
            action = '<span class="sold">Sold out</span>'
        elif accepting:
            action = '<button class="btn" data-add="%s">Add to cart</button>' % e(p["id"])
        else:
            action = '<span class="sold">Ordering paused</span>'
        few = ""
        if ok and p.get("stock") is not None and p["stock"] <= 3:
            few = '<p class="few">Only %d left</p>' % p["stock"]
        neg = ""
        if wa and ok:
            text = "Hello %s, I would like to negotiate the price of the %s (%s)." % (shop["name"], p["name"], money(p["price"]))
            neg = '<a class="neg" href="https://wa.me/%s?text=%s" target="_blank" rel="noopener">Negotiate on WhatsApp</a>' % (
                wa, quote(text, safe=""))
        desc = '<p class="d">%s</p>' % e(p["description"]) if p.get("description") else ""
        cards.append(
            '<article class="card" data-cat="%s" data-name="%s">%s<div class="b"><div class="cat">%s</div><h3>%s</h3>%s'
            '<div class="price">%s</div>%s%s%s</div></article>' % (
                e(p["category"]), e(p["name"].lower()), img, e(p["category"]), e(p["name"]), desc,
                e(money(p["price"])), few, action, neg))
    body = '<div class="grid">%s</div>' % "".join(cards) if cards else '<p class="empty">New items are coming soon.</p>'
    filters = ""
    if products:
        chips = ""
        if len(used) > 1:
            chips = '<button class="chip" data-cat="All" aria-pressed="true">All</button>' + "".join(
                '<button class="chip" data-cat="%s" aria-pressed="false">%s</button>' % (e(c), e(c)) for c in used)
        search = '<input id="q" type="search" placeholder="Search" aria-label="Search the shop">' if len(products) > 3 else ""
        if chips or search:
            filters = '<div class="filters">%s%s</div>' % (search, chips)
    buttons = ""
    if wa:
        buttons += '<a class="btn ghost" href="https://wa.me/%s?text=%s" target="_blank" rel="noopener">WhatsApp</a>' % (
            wa, quote("Hello %s, I have a question." % shop["name"], safe=""))
    if email:
        buttons += '<a class="btn ghost" href="mailto:%s">Email</a>' % e(email)
    contact = ('<section class="contact"><h2>Want a better price?</h2><p>Message us to negotiate or ask anything.</p>%s</section>' % buttons) if buttons else ""
    banner = shop.get("banner", "")
    hero_style = ""
    if banner and re.fullmatch(IMG_RE, banner):
        hero_style = ' style="background-image:linear-gradient(rgba(5,14,36,.68),rgba(5,14,36,.93)),url(/img/%s)"' % e(banner)
    catalog = json.dumps({
        "shop": {"name": shop["name"], "wa": wa, "email": email, "accepting": accepting},
        "products": [{"id": p["id"], "name": p["name"], "price": p["price"],
                      "max": min(10, p["stock"]) if p.get("stock") is not None else 10}
                     for p in products if in_stock(p)],
    }).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return fill(PUBLIC_TEMPLATE, {
        "TITLE": e(shop["name"]), "FONTS": FONT_LINK, "CSS": PUBLIC_CSS, "HEROSTYLE": hero_style,
        "SHOP": e(shop["name"]), "TAGLINE": e(" \u00b7 ".join(used) if used else "Fine pieces"),
        "BANNER": "" if accepting else '<p class="pause">Orders are paused right now. Message us on WhatsApp.</p>',
        "FILTERS": filters, "BODY": body, "CONTACT": contact, "ADDRESS": e(shop["address"]),
        "EMAILLINE": ('<br><a href="mailto:%s">%s</a>' % (e(email), e(email))) if email else "",
        "CATALOG": catalog, "JS": PUBLIC_JS,
    })


# ---------- owner site ----------
ADMIN_HTML = r"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Owner area</title>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@500;600;700&family=Jost:wght@300;400;500&display=swap" rel="stylesheet">
<style>
[hidden]{display:none!important}
*{box-sizing:border-box}
:focus-visible{outline:2px solid #d4b46a;outline-offset:3px}
body{margin:0;background:radial-gradient(1000px 480px at 50% -160px,#1a4690,#07142e 62%) #07142e;color:#eef3ff;font:300 16px/1.6 Jost,"Segoe UI",system-ui,sans-serif;min-height:100vh}
.w{max-width:800px;margin:0 auto;padding:28px 20px}
h1{font:600 38px/1.1 "Cormorant Garamond",Georgia,serif;margin:0;letter-spacing:.02em}
h2{font:600 25px "Cormorant Garamond",Georgia,serif;margin:0 0 10px;color:#ecd59a}
.box{background:#0d2147;border:1px solid #1f3a6e;padding:22px;margin:18px 0}
label{display:block;font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:#9fb3d6;margin:14px 0 6px}
input,textarea,select{width:100%;padding:12px;border:1px solid #2b4a85;background:#07142e;color:inherit;font:inherit}
input:focus,textarea:focus,select:focus{border-color:#d4b46a}
input[type=checkbox]{width:auto}
button,a.b{background:linear-gradient(135deg,#ecd59a,#b18a3c);color:#1a1405;border:0;border-radius:0;padding:11px 22px;font:500 13px Jost,sans-serif;letter-spacing:.12em;text-transform:uppercase;cursor:pointer;text-decoration:none;display:inline-block}
button.g,a.b.g{background:transparent;color:#eef3ff;border:1px solid #2b4a85}
button.r{background:transparent;color:#ff9f9f;border:1px solid #ff9f9f}
button.s{padding:7px 12px;font-size:12px;letter-spacing:.06em}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:14px}
.top{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}
.item{display:flex;gap:14px;align-items:center;padding:14px 0;border-top:1px solid #1f3a6e;flex-wrap:wrap}
.item img,.noimg{width:68px;height:68px;object-fit:cover;background:#12295a;display:flex;align-items:center;justify-content:center;font-size:11px;color:#7388b0;outline:1px solid rgba(212,180,106,.35);outline-offset:-5px}
.meta{flex:1;min-width:150px}.acts{display:flex;gap:6px;flex-wrap:wrap}
.msg{color:#d4b46a;font-size:14px;min-height:20px;margin:8px 0 0}
.hint{color:#9fb3d6;font-size:13px;margin:10px 0 0}
.gold{color:#d4b46a;text-transform:none;letter-spacing:0;font-size:14px}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.qp{width:140px;padding:8px}
.inl{margin:6px 0 0}
.two{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media (max-width:560px){.two{grid-template-columns:1fr}}
.order{border:1px solid #1f3a6e;background:#0a1b3d;padding:16px;margin:12px 0}
.ohead{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}
.badge{font-size:11px;letter-spacing:.14em;text-transform:uppercase;padding:3px 10px;border:1px solid}
.badge.open{color:#d4b46a;border-color:#d4b46a}.badge.closed{color:#9fb3d6;border-color:#2b4a85}
.ol{padding:3px 0}.tot{font:600 22px "Cormorant Garamond",Georgia,serif;color:#ecd59a;margin-top:6px}
.sub{color:#9fb3d6;font-size:14px}
</style></head><body><div class="w">

<div id="login" hidden><div class="box">
<h1>Owner login</h1>
<label for="pw">Password</label>
<input id="pw" type="password" autocomplete="current-password">
<div class="row"><button onclick="login()">Log in</button></div>
<p class="msg" id="lmsg"></p></div></div>

<div id="dash" hidden>
<div class="top"><h1>Owner area</h1><div class="row" style="margin:0">
<a class="b" id="pubopen" href="#" target="_blank" rel="noopener">Open public shop</a><button class="g" onclick="logout()">Log out</button></div></div>

<div class="box"><h2>Orders</h2>
<div class="row" style="margin:0 0 6px"><button id="tabopen" onclick="setTab('open')">Open</button><button id="tabclosed" class="g" onclick="setTab('closed')">Closed</button></div>
<div id="orders"></div></div>

<div class="box"><h2>Share your shop</h2>
<label for="sharelink">Link for customers</label>
<input id="sharelink" readonly>
<div class="row"><button onclick="copyLink()">Copy link</button><a class="b g" id="sharewa" href="#" target="_blank" rel="noopener">Send on WhatsApp</a></div>
<p class="hint" id="sharehint"></p><p class="msg" id="copymsg"></p></div>

<div class="box"><h2>Shop details</h2>
<div class="two"><div><label for="sname">Shop name</label><input id="sname" maxlength="40"></div>
<div><label for="swa">WhatsApp number (customers negotiate here)</label><input id="swa" placeholder="08012345678" inputmode="tel"></div></div>
<div class="two"><div><label for="semail">Email (customers can write to you)</label><input id="semail" type="email" maxlength="80"></div>
<div><label for="saddr">Address</label><input id="saddr" maxlength="120"></div></div>
<label for="scats">Item categories (separate with commas)</label><input id="scats" placeholder="Watches, Shoes, Shades, Gadgets, Accessories">
<label for="surl">Your internet shop address (optional, paste it here once your shop is online)</label><input id="surl" placeholder="https://...">
<label for="sbanner">Banner photo for the top of your shop (optional)</label><input id="sbanner" type="file" accept="image/*">
<p class="hint" id="bstat"></p>
<label><input id="sclear" type="checkbox"> Remove the banner photo</label>
<label><input id="sopen" type="checkbox"> Accepting orders (untick to pause the cart)</label>
<div class="row"><button onclick="saveSettings()">Save details</button></div><p class="msg" id="smsg"></p></div>

<div class="box"><h2 id="ptitle">Add an item</h2>
<label for="pname">Item name</label><input id="pname" maxlength="80">
<div class="two"><div><label for="pcat">Category</label><select id="pcat"></select></div>
<div><label for="pstock">How many in stock (optional)</label><input id="pstock" type="number" min="0" step="1" inputmode="numeric" placeholder="Leave empty if unlimited"></div></div>
<label for="pprice">Price in naira <span class="gold" id="pshow"></span></label>
<input id="pprice" type="number" min="0" step="any" inputmode="decimal" oninput="showPrice()">
<div class="chips" id="pchips"></div>
<label for="pdesc">Short description (optional)</label><textarea id="pdesc" rows="2" maxlength="300"></textarea>
<label for="pfile">Photo (optional)</label><input id="pfile" type="file" accept="image/*">
<label><input id="pavail" type="checkbox" checked> Available</label>
<div class="row"><button onclick="saveProduct()">Save item</button><button id="pcancel" class="g" onclick="resetForm()" hidden>Cancel</button></div>
<p class="hint">Shortcuts: Ctrl+Enter saves, Alt+N starts a new item, Esc cancels an edit.</p>
<p class="msg" id="pmsg"></p></div>

<div class="box"><h2>Your items</h2><div id="list"></div></div>

<div class="box"><h2>Backup</h2>
<p class="sub">The shop saves a backup of your items, orders and settings by itself and keeps the last 30 in the data folder. You can also download one now.</p>
<div class="row"><a class="b g" href="/api/admin/export">Download backup</a></div></div>
</div></div>

<script>
var S={settings:{categories:[]},products:[],orders:[],links:{}},editing=null,tab="open",lastOpen=null,PUB="@PUBPORT@";
function $(i){return document.getElementById(i)}
function el(t,p){var e=document.createElement(t);for(var k in (p||{})){if(k==="text")e.textContent=p[k];else if(k.slice(0,2)==="on")e[k]=p[k];else e.setAttribute(k,p[k]);}
 for(var i=2;i<arguments.length;i++){var c=arguments[i];if(c)e.appendChild(typeof c==="string"?document.createTextNode(c):c);}return e}
function money(n){return "\u20a6"+Number(n).toLocaleString()}
function waDigits(n){var d=String(n||"").replace(/\D/g,"");return d.charAt(0)==="0"?"234"+d.slice(1):d}
function api(path,body){return fetch(path,{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json"},body:JSON.stringify(body||{})})
 .then(function(r){return r.json().catch(function(){return {}}).then(function(d){return {ok:r.ok,status:r.status,data:d}})})}
function show(w){$("login").hidden=w!=="login";$("dash").hidden=w!=="dash"}
function pull(){return fetch("/api/admin/state",{credentials:"same-origin"}).then(function(r){if(r.status===401){show("login");return null}return r.json()})}
function load(){return pull().then(function(d){if(d){S=d;draw();drawOrders();drawShare();show("dash")}})}
function poll(){if($("dash").hidden)return;pull().then(function(d){if(d){S.orders=d.orders;drawOrders()}})}
setInterval(poll,20000);
function login(){api("/api/login",{password:$("pw").value}).then(function(r){if(r.ok){$("pw").value="";$("lmsg").textContent="";load()}else{$("lmsg").textContent=r.data.error||"Could not log in."}})}
function logout(){api("/api/logout").then(function(){show("login")})}
function beep(){try{var a=new (window.AudioContext||window.webkitAudioContext)(),o=a.createOscillator(),g=a.createGain();o.connect(g);g.connect(a.destination);o.frequency.value=880;g.gain.value=0.1;o.start();setTimeout(function(){o.stop();a.close()},350)}catch(e){}}

function pubBase(){return location.protocol+"//"+location.hostname+":"+PUB}
function shareUrl(){return S.settings.public_url||(S.links&&S.links.lan)||pubBase()}
function drawShare(){var u=shareUrl();$("sharelink").value=u;$("pubopen").href=pubBase();
 $("sharewa").href="https://wa.me/?text="+encodeURIComponent("Shop with us: "+u);
 $("sharehint").textContent=S.settings.public_url?"This is your internet address. Anyone can open it.":"This link works for phones on the same Wi-Fi as this computer. To share it with customers anywhere, put your shop online and paste its address in Shop details."}
function copyLink(){var i=$("sharelink");i.select();var ok=false;
 try{if(navigator.clipboard){navigator.clipboard.writeText(i.value);ok=true}else{ok=document.execCommand("copy")}}catch(e){try{ok=document.execCommand("copy")}catch(e2){}}
 $("copymsg").textContent=ok?"Link copied.":"Press Ctrl+C to copy the link."}

function setTab(t){tab=t;drawOrders()}
function setStatus(o){api("/api/order/status",{id:o.id,status:o.status==="open"?"closed":"open"}).then(poll)}
function delOrder(o){if(confirm("Delete order #"+o.id+" for good?"))api("/api/order/delete",{id:o.id}).then(poll)}
function drawOrders(){
 var open=S.orders.filter(function(o){return o.status==="open"}),closed=S.orders.filter(function(o){return o.status!=="open"});
 if(lastOpen!==null&&open.length>lastOpen)beep();lastOpen=open.length;
 document.title=(open.length?"("+open.length+") ":"")+"Owner area";
 $("tabopen").textContent="Open ("+open.length+")";$("tabclosed").textContent="Closed ("+closed.length+")";
 $("tabopen").className=tab==="open"?"":"g";$("tabclosed").className=tab==="closed"?"":"g";
 var list=$("orders");list.innerHTML="";var rows=tab==="open"?open:closed;
 if(!rows.length)list.appendChild(el("p",{class:"msg",text:tab==="open"?"No open orders right now.":"No closed orders yet."}));
 rows.forEach(function(o){
  var wa=waDigits(o.phone),msg="Hello "+o.name+", this is "+S.settings.name+". About your order #"+o.id+" ("+money(o.total)+").";
  var box=el("div",{class:"order"},
   el("div",{class:"ohead"},el("strong",{text:"#"+o.id+" \u00b7 "+o.name}),el("span",{class:"badge "+o.status,text:o.status==="open"?"Open":"Closed"})),
   el("div",{class:"sub",text:o.created+" \u00b7 "+o.phone}));
  o.items.forEach(function(i){box.appendChild(el("div",{class:"ol",text:i.qty+" x "+i.name+" \u00b7 "+money(i.price)}))});
  box.appendChild(el("div",{class:"tot",text:"Total "+money(o.total)}));
  if(o.note)box.appendChild(el("div",{class:"sub",text:"Note: "+o.note}));
  box.appendChild(el("div",{class:"row"},
   wa?el("a",{class:"b",href:"https://wa.me/"+wa+"?text="+encodeURIComponent(msg),target:"_blank",rel:"noopener",text:"WhatsApp customer"}):null,
   el("button",{class:"g",text:o.status==="open"?"Close order":"Re-open order",onclick:function(){setStatus(o)}}),
   el("button",{class:"r",text:"Delete",onclick:function(){delOrder(o)}})));
  list.appendChild(box)})}

function saveSettings(){var msg=$("smsg");msg.textContent="";
 var f=$("sbanner").files[0],p=f?shrink(f,1600):Promise.resolve("");
 p.then(function(img){return api("/api/settings",{name:$("sname").value,whatsapp:$("swa").value,email:$("semail").value,address:$("saddr").value,
  categories:$("scats").value,public_url:$("surl").value,accepting:$("sopen").checked,banner:img,clear_banner:$("sclear").checked})})
 .then(function(r){msg.textContent=r.ok?"Saved.":(r.data.error||"Could not save.");if(r.ok){$("sbanner").value="";$("sclear").checked=false;load()}})
 .catch(function(){msg.textContent="That photo could not be read. Try a JPG or PNG."})}
function shrink(file,max){return new Promise(function(res,rej){var img=new Image(),u=URL.createObjectURL(file);
 img.onload=function(){var s=Math.min(1,max/Math.max(img.width,img.height)),c=document.createElement("canvas");
 c.width=Math.round(img.width*s);c.height=Math.round(img.height*s);c.getContext("2d").drawImage(img,0,0,c.width,c.height);
 URL.revokeObjectURL(u);res(c.toDataURL("image/jpeg",0.85))};img.onerror=rej;img.src=u})}

var PRESETS=[5000,10000,15000,20000,25000,30000,50000,100000],ADDS=[1000,5000,10000];
function kfmt(v){return "\u20a6"+(v>=1000?v/1000+"k":v)}
function showPrice(){var v=Number($("pprice").value);$("pshow").textContent=v>0?"= "+money(v):""}
function setPrice(v){$("pprice").value=Math.max(0,v);showPrice()}
(function(){var c=$("pchips");
 PRESETS.forEach(function(v){c.appendChild(el("button",{class:"g s",text:kfmt(v),onclick:function(){setPrice(v)}}))});
 ADDS.forEach(function(v){c.appendChild(el("button",{class:"g s",text:"+"+(v/1000)+"k",onclick:function(){setPrice((Number($("pprice").value)||0)+v)}}))});
 c.appendChild(el("button",{class:"g s",text:"Clear",onclick:function(){setPrice(0);$("pprice").value=""}}))})();

function fillCats(sel){var s=$("pcat"),cats=(S.settings.categories||[]).slice();if(sel&&cats.indexOf(sel)<0)cats.push(sel);
 s.innerHTML="";cats.forEach(function(c){s.appendChild(el("option",{value:c,text:c}))});if(sel)s.value=sel}
function resetForm(){editing=null;$("ptitle").textContent="Add an item";$("pname").value="";$("pprice").value="";$("pdesc").value="";$("pstock").value="";$("pavail").checked=true;$("pfile").value="";$("pcancel").hidden=true;fillCats();showPrice()}
function fillForm(p){$("pname").value=p.name;$("pprice").value=p.price;$("pdesc").value=p.description||"";$("pstock").value=p.stock==null?"":p.stock;$("pavail").checked=!!p.available;$("pfile").value="";$("pmsg").textContent="";fillCats(p.category);showPrice();window.scrollTo(0,$("ptitle").offsetTop-20)}
function edit(p){editing=p.id;fillForm(p);$("ptitle").textContent="Edit item";$("pcancel").hidden=false}
function dup(p){resetForm();fillForm(p);$("pname").value=p.name+" (copy)";$("pmsg").textContent="Copy started. Change the name or price, add a photo, then save."}
function pbody(p,o){var b={id:p.id,name:p.name,price:p.price,description:p.description||"",available:p.available,category:p.category,stock:p.stock==null?"":p.stock,image:""};for(var k in (o||{}))b[k]=o[k];return b}
function saveProduct(){var msg=$("pmsg");msg.textContent="";
 var body={id:editing||"",name:$("pname").value,price:$("pprice").value,description:$("pdesc").value,available:$("pavail").checked,category:$("pcat").value,stock:$("pstock").value,image:""};
 var f=$("pfile").files[0];var p=f?shrink(f,1000):Promise.resolve("");
 p.then(function(img){body.image=img;return api("/api/product/save",body)}).then(function(r){
  if(!r.ok){msg.textContent=r.data.error||"Could not save.";return}
  resetForm();msg.textContent="Saved.";load()}).catch(function(){msg.textContent="That photo could not be read. Try a JPG or PNG."})}
function toggle(p){api("/api/product/save",pbody(p,{available:!p.available})).then(load)}
function quickPrice(p,v){if(v===""||!(Number(v)>=0)){alert("Enter a valid price.");load();return}
 api("/api/product/save",pbody(p,{price:v})).then(function(r){if(!r.ok)alert(r.data.error||"Could not save the price.");load()})}
function del(p){if(confirm("Delete "+p.name+"?"))api("/api/product/delete",{id:p.id}).then(load)}
function draw(){var st=S.settings,cur=$("pcat").value;
 $("sname").value=st.name||"";$("swa").value=st.whatsapp||"";$("semail").value=st.email||"";$("saddr").value=st.address||"";
 $("scats").value=(st.categories||[]).join(", ");$("surl").value=st.public_url||"";$("sopen").checked=st.accepting!==false;
 $("bstat").textContent=st.banner?"A banner photo is set. Choose a new one to replace it.":"No banner photo yet. The shop uses the luxury blue background.";
 fillCats(cur);
 var list=$("list");list.innerHTML="";
 if(!S.products.length)list.appendChild(el("p",{class:"msg",text:"No items yet. Add your first one above."}));
 S.products.forEach(function(p){
  var th=p.image?el("img",{src:"/img/"+p.image,alt:""}):el("div",{class:"noimg",text:"No photo"});
  var qp=el("input",{class:"qp",type:"number",min:"0",step:"any",value:String(p.price),"aria-label":"Price for "+p.name});
  qp.onchange=function(){quickPrice(p,qp.value)};
  list.appendChild(el("div",{class:"item"},th,
   el("div",{class:"meta"},el("strong",{text:p.name}),
    el("div",{class:"sub",text:(p.category||"")+" \u00b7 "+(p.available?"Available":"Sold out")+(p.stock==null?"":" \u00b7 "+p.stock+" in stock")}),
    el("label",{class:"inl"},"Price \u20a6 ",qp)),
   el("div",{class:"acts"},
    el("button",{class:"g s",text:"Edit",onclick:function(){edit(p)}}),
    el("button",{class:"g s",text:"Copy",onclick:function(){dup(p)}}),
    el("button",{class:"g s",text:p.available?"Mark sold out":"Mark available",onclick:function(){toggle(p)}}),
    el("button",{class:"r s",text:"Delete",onclick:function(){del(p)}}))))})}

$("pw").addEventListener("keydown",function(ev){if(ev.key==="Enter")login()});
document.addEventListener("keydown",function(ev){if($("dash").hidden)return;
 if((ev.ctrlKey||ev.metaKey)&&ev.key==="Enter"){ev.preventDefault();saveProduct()}
 else if(ev.key==="Escape"&&editing){resetForm()}
 else if(ev.altKey&&ev.key.toLowerCase()==="n"){ev.preventDefault();resetForm();$("pname").focus();window.scrollTo(0,$("ptitle").offsetTop-20)}});
load();
</script></body></html>"""


def admin_page():
    return ADMIN_HTML.replace("@PUBPORT@", str(PUBLIC_PORT))


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


# ---------- web servers ----------
def guarded(fn):
    def wrapper(self):
        try:
            fn(self)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception as err:
            print("Problem while answering a request:", err)
            try:
                self.send_json(500, {"error": "Something went wrong. Please try again."})
            except Exception:
                self.close_connection = True
    return wrapper


class Handler(BaseHTTPRequestHandler):
    server_version = "FarukShop"
    timeout = 30
    owner = False

    def log_message(self, *args):
        pass

    def send_body(self, code, body, ctype, extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, code, obj, extra=None):
        self.send_body(code, json.dumps(obj), "application/json", [("Cache-Control", "no-store")] + (extra or []))

    def is_admin(self):
        try:
            morsel = SimpleCookie(self.headers.get("Cookie", "")).get("session")
        except Exception:
            return False
        if not morsel:
            return False
        expires = SESSIONS.get(morsel.value)
        if not expires or expires < time.time():
            SESSIONS.pop(morsel.value, None)
            return False
        return True

    @guarded
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        nocache = [("Cache-Control", "no-store")]
        if self.owner:
            if path in ("/", "/admin"):
                return self.send_body(200, admin_page(), "text/html; charset=utf-8", nocache)
            if path == "/api/admin/state":
                if not self.is_admin():
                    return self.send_json(401, {"error": "Log in first."})
                return self.send_json(200, {
                    "settings": load_config()["shop"], "products": load_products(), "orders": load_orders(),
                    "links": {"lan": "http://%s:%d" % (lan_ip(), PUBLIC_PORT)}})
            if path == "/api/admin/export":
                if not self.is_admin():
                    return self.send_json(401, {"error": "Log in first."})
                payload = json.dumps({"saved": time.strftime("%Y-%m-%d %H:%M"), "settings": load_config()["shop"],
                                      "products": load_products(), "orders": load_orders()},
                                     ensure_ascii=False, indent=2)
                return self.send_body(200, payload, "application/json; charset=utf-8", [
                    ("Content-Disposition", 'attachment; filename="shop-backup-%s.json"' % time.strftime("%Y-%m-%d")),
                    ("Cache-Control", "no-store")])
        else:
            if path == "/":
                return self.send_body(200, public_page(), "text/html; charset=utf-8", nocache)
            if path == "/health":
                return self.send_body(200, "ok", "text/plain")
        m = re.fullmatch(r"/img/(" + IMG_RE + ")", path)
        if m:
            fp = os.path.join(IMG_DIR, m.group(1))
            if os.path.isfile(fp):
                with open(fp, "rb") as f:
                    return self.send_body(200, f.read(), "image/jpeg",
                                          [("Cache-Control", "public, max-age=31536000, immutable")])
        self.send_body(404, "Not found", "text/plain")

    @guarded
    def do_POST(self):
        path = self.path.split("?", 1)[0]
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != (self.headers.get("Host") or ""):
            return self.send_json(403, {"error": "Blocked."})
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if ctype != "application/json" or n < 0 or n > (MAX_BODY if self.owner else 64 * 1024):
            return self.send_json(400, {"error": "Bad request."})
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(data, dict):
                raise ValueError
        except ValueError:
            return self.send_json(400, {"error": "Bad request."})
        if not self.owner:
            if path == "/api/order":
                return self.place_order(data)
            return self.send_json(404, {"error": "Not found."})
        if path == "/api/login":
            return self.login(data)
        if not self.is_admin():
            return self.send_json(401, {"error": "Log in first."})
        try:
            if path == "/api/logout":
                morsel = SimpleCookie(self.headers.get("Cookie", "")).get("session")
                SESSIONS.pop(morsel.value if morsel else "", None)
                return self.send_json(200, {"ok": True},
                                      [("Set-Cookie", "session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")])
            if path == "/api/settings":
                save_settings(data)
                return self.send_json(200, {"ok": True})
            if path == "/api/product/save":
                return self.send_json(200, {"ok": True, "product": save_product(data)})
            if path == "/api/product/delete":
                delete_product(data.get("id", ""))
                return self.send_json(200, {"ok": True})
            if path == "/api/order/status":
                set_order_status(data.get("id", ""), data.get("status", ""))
                return self.send_json(200, {"ok": True})
            if path == "/api/order/delete":
                delete_order(data.get("id", ""))
                return self.send_json(200, {"ok": True})
        except ValueError as err:
            return self.send_json(400, {"error": str(err)})
        self.send_json(404, {"error": "Not found."})

    def login(self, data):
        now = time.time()
        FAILS[:] = [t for t in FAILS if now - t < 60]
        if len(FAILS) >= 5:
            return self.send_json(429, {"error": "Too many tries. Wait a minute."})
        if check_password(str(data.get("password", ""))):
            token = secrets.token_urlsafe(32)
            SESSIONS[token] = now + 86400
            cookie = "session=%s; HttpOnly; SameSite=Strict; Path=/; Max-Age=86400" % token
            return self.send_json(200, {"ok": True}, [("Set-Cookie", cookie)])
        FAILS.append(now)
        time.sleep(1)
        self.send_json(401, {"error": "Wrong password."})

    def place_order(self, data):
        ip = self.client_address[0]
        now = time.time()
        recent = [t for t in ORDER_TIMES.get(ip, []) if now - t < 600]
        if len(recent) >= 5:
            return self.send_json(429, {"error": "Too many orders. Please wait a few minutes or message us on WhatsApp."})
        try:
            order = create_order(data)
        except ValueError as err:
            return self.send_json(400, {"error": str(err)})
        recent.append(now)
        ORDER_TIMES[ip] = recent
        self.send_json(200, {"ok": True, "order": {k: order[k] for k in ("id", "name", "items", "total")}})


class OwnerHandler(Handler):
    owner = True


class PublicHandler(Handler):
    owner = False


def main():
    os.makedirs(IMG_DIR, exist_ok=True)
    first_run_setup()
    backup_now(force=True)
    try:
        owner_server = ThreadingHTTPServer(("127.0.0.1", OWNER_PORT), OwnerHandler)
    except OSError:
        print("Port %d is busy. Close any other copy of this program, or change OWNER_PORT at the top." % OWNER_PORT)
        return
    public_ok = True
    try:
        public_server = ThreadingHTTPServer((PUBLIC_HOST, PUBLIC_PORT), PublicHandler)
        threading.Thread(target=public_server.serve_forever, daemon=True).start()
    except OSError:
        public_ok = False
        print("Warning: port %d is busy, so the public shop did not start. Change PUBLIC_PORT at the top." % PUBLIC_PORT)
    print("Owner area (private, only you):  http://localhost:%d" % OWNER_PORT)
    if public_ok:
        print("Public shop on this computer:    http://localhost:%d" % PUBLIC_PORT)
        if PUBLIC_HOST != "127.0.0.1":
            print("Public shop on your Wi-Fi:       http://%s:%d" % (lan_ip(), PUBLIC_PORT))
    print("Press Ctrl+C to stop.")
    webbrowser.open("http://localhost:%d" % OWNER_PORT)
    try:
        owner_server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")
        owner_server.server_close()


if __name__ == "__main__":
    main()
