from __future__ import annotations

from collections.abc import Callable
from html import escape
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from effect_browser.store import DatabaseStore


def create_demo_router(store_provider: Callable[[], DatabaseStore]) -> APIRouter:
    router = APIRouter()

    @router.get("/demo-shop", response_class=HTMLResponse)
    def shop() -> str:
        return _page(
            """
            <p class="eyebrow">Synthetic target · real side effect</p>
            <h1>Warehouse order desk</h1>
            <p class="lede">This portal has no API. The customer reference is the only
            stable key available for outcome reconciliation.</p>
            <form method="post" action="/demo-shop/orders">
              <label>Product
                <select name="product">
                  <option value="backup-drive">Encrypted backup drive</option>
                  <option value="dock">USB-C dock</option>
                </select>
              </label>
              <label>Quantity
                <input name="quantity" type="number" min="1" max="20" value="1" />
              </label>
              <label>Customer reference
                <input name="reference" required
                  placeholder="Stable external reference" />
              </label>
              <button type="submit">Place order</button>
            </form>
            """
        )

    @router.post("/demo-shop/orders", response_class=HTMLResponse)
    async def place_order(request: Request) -> str:
        form = parse_qs((await request.body()).decode("utf-8"))
        product = form.get("product", [""])[0]
        reference = form.get("reference", [""])[0].strip()
        try:
            quantity = int(form.get("quantity", ["0"])[0])
        except ValueError as exc:
            raise HTTPException(422, "quantity must be an integer") from exc
        if not reference or not product or not 1 <= quantity <= 20:
            raise HTTPException(
                422, "product, reference, and valid quantity are required"
            )
        order_id, created = store_provider().create_demo_order(
            reference=reference,
            product=product,
            quantity=quantity,
        )
        verb = "created" if created else "already existed"
        return _page(
            f"""
            <p class="eyebrow">Remote commit {verb}</p>
            <h1>Order receipt</h1>
            <div class="receipt" data-testid="receipt">
              <strong>{escape(reference)}</strong>
              <span>Order {escape(order_id)}</span>
              <span>{quantity} × {escape(product)}</span>
            </div>
            <a href="/demo-shop/orders?reference={escape(reference)}">Look up order</a>
            """
        )

    @router.get("/demo-shop/orders", response_class=HTMLResponse)
    def find_order(reference: str = "") -> str:
        order = store_provider().demo_order(reference) if reference else None
        if not order:
            content = "<h1>No matching order</h1><p>The reference has not committed.</p>"
        else:
            content = f"""
              <p class="eyebrow">Deterministic reconciliation result</p>
              <h1>Existing order</h1>
              <div class="receipt" data-testid="receipt"
                data-external-id="{escape(order["id"])}">
                <strong>{escape(order["reference"])}</strong>
                <span>Order {escape(order["id"])}</span>
                <span>{order["quantity"]} × {escape(order["product"])}</span>
                <span>Duplicate attempts: {order["duplicate_attempts"]}</span>
              </div>
            """
        return _page(content)

    @router.get("/demo-shop/api/orders")
    def orders() -> list[dict]:
        return store_provider().demo_orders()

    return router


def _page(content: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport"
content="width=device-width,initial-scale=1"><title>Effect Browser Demo Shop</title>
<style>
:root{{--ink:#14221f;--muted:#66736f;--paper:#fffefa;--accent:#e2643f;}}
*{{box-sizing:border-box}} body{{max-width:760px;margin:0 auto;padding:72px 28px;
font-family:system-ui,sans-serif;color:var(--ink);background:#eef0ea}}
h1{{font:500 48px Georgia,serif;margin:8px 0 16px;letter-spacing:-.04em}}
.eyebrow{{font-size:11px;text-transform:uppercase;letter-spacing:.16em;color:var(--muted)}}
.lede{{max-width:620px;color:var(--muted);line-height:1.6}} form,.receipt{{display:grid;
gap:16px;margin-top:32px;padding:28px;border:1px solid #d5dcd5;border-radius:10px;
background:var(--paper);box-shadow:0 16px 45px #14221f12}} label{{display:grid;gap:7px;
font-size:12px;font-weight:700}} input,select{{padding:13px;border:1px solid #bac5be;
border-radius:6px;background:white;font:inherit}} button{{padding:14px;border:0;
border-radius:6px;color:white;background:var(--ink);font-weight:750;cursor:pointer}}
.receipt span{{color:var(--muted)}}
a{{display:inline-block;margin-top:22px;color:var(--ink)}}
</style></head><body>{content}</body></html>"""
