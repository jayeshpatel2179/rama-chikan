/**
 * Rama Chikan — Shopify Web Pixel: GA4 dataLayer bridge for checkout events
 * (2026-09-24)
 *
 * WHAT THIS IS
 * Shopify's storefront theme (theme.liquid) cannot see checkout/thank-you
 * page events at all -- those pages are outside the theme entirely. The
 * only supported way to track them (on a Basic-plan store, with no
 * "Additional scripts" box) is a Shopify Web Pixel: a small sandboxed
 * script registered through Admin -> Settings -> Customer events, which
 * Shopify runs on every storefront AND checkout page and feeds real
 * event data into via `analytics.subscribe(...)`.
 *
 * This file subscribes to the 4 standard Shopify events that cover the
 * funnel from product view through to a completed order, and re-emits
 * each one as a GA4-shaped push to `window.dataLayer` -- but that
 * `dataLayer` only exists inside THIS pixel's own sandboxed execution
 * context, not the one on your real storefront pages. That's what the
 * GTM loader below is for: it creates a fresh GTM instance running
 * inside this same sandbox, so it has a dataLayer to read from.
 *
 * FIELD SOURCES -- every field below comes directly from Shopify's own
 * documented Web Pixels API event payload shape (Standard events:
 * product_viewed, product_added_to_cart, checkout_started,
 * checkout_completed). Nothing here is invented; see the inline
 * references at each mapping.
 *
 * MANUAL STEP REQUIRED (not done by this file, not pushed anywhere):
 *   1. Replace GTM-XXXXXXX below with your real GTM container ID.
 *   2. Paste this entire file's contents into:
 *      Shopify Admin -> Settings -> Customer events -> Add custom pixel
 *   3. Save, then confirm it in Customer events' own connection status.
 *
 * item_id below uses the VARIANT id (not the product id) -- the specific
 * purchasable SKU, which is the more common convention for GA4 ecommerce
 * reporting on Shopify. If your GA4/Ads setup expects the product id
 * instead, swap `variant.id` for `variant.product.id` in the three
 * places it's used.
 */

// --- Google Tag Manager loader (runs inside this pixel's own sandbox) ---
// Replace GTM-XXXXXXX with the real container ID before pasting this
// anywhere -- see the module comment above.
(function (w, d, s, l, i) {
  w[l] = w[l] || [];
  w[l].push({ 'gtm.start': new Date().getTime(), event: 'gtm.js' });
  var f = d.getElementsByTagName(s)[0],
    j = d.createElement(s),
    dl = l != 'dataLayer' ? '&l=' + l : '';
  j.async = true;
  j.src = 'https://www.googletagmanager.com/gtm.js?id=' + i + dl;
  f.parentNode.insertBefore(j, f);
})(window, document, 'script', 'dataLayer', 'GTM-XXXXXXX');

window.dataLayer = window.dataLayer || [];

/**
 * Formats a Shopify Web Pixels `MoneyV2`-shaped value
 * ({ amount: string, currencyCode: string }) into a plain float for GA4's
 * `value`/`price` fields, which expect numbers, not money strings.
 */
function toAmount(money) {
  return money ? parseFloat(money.amount) : undefined;
}

/**
 * Clearing `ecommerce` before every push is GA4/GTM's own documented
 * recommendation -- without it, array fields (like `items`) from a
 * previous push can bleed into the next one instead of being replaced.
 */
function pushEcommerce(event, ecommerce) {
  window.dataLayer.push({ ecommerce: null });
  window.dataLayer.push({ event: event, ecommerce: ecommerce });
}

// --- product_viewed -> view_item ---------------------------------------
// Payload: event.data.productVariant (Shopify's ProductVariant type)
analytics.subscribe('product_viewed', (event) => {
  const variant = event.data.productVariant;
  if (!variant) return;

  pushEcommerce('view_item', {
    currency: variant.price.currencyCode,
    value: toAmount(variant.price),
    items: [
      {
        item_id: variant.id,
        item_name: variant.product.title,
        price: toAmount(variant.price),
        quantity: 1,
      },
    ],
  });
});

// --- product_added_to_cart -> add_to_cart -------------------------------
// Payload: event.data.cartLine (Shopify's CartLine type)
analytics.subscribe('product_added_to_cart', (event) => {
  const cartLine = event.data.cartLine;
  if (!cartLine) return;
  const variant = cartLine.merchandise;

  pushEcommerce('add_to_cart', {
    currency: cartLine.cost.totalAmount.currencyCode,
    value: toAmount(cartLine.cost.totalAmount),
    items: [
      {
        item_id: variant.id,
        item_name: variant.product.title,
        price: toAmount(variant.price),
        quantity: cartLine.quantity,
      },
    ],
  });
});

// --- checkout_started -> begin_checkout ---------------------------------
// Payload: event.data.checkout (Shopify's Checkout type)
analytics.subscribe('checkout_started', (event) => {
  const checkout = event.data.checkout;
  if (!checkout) return;

  pushEcommerce('begin_checkout', {
    currency: checkout.totalPrice.currencyCode,
    value: toAmount(checkout.totalPrice),
    items: (checkout.lineItems || []).map((lineItem) => ({
      item_id: lineItem.variant.id,
      item_name: lineItem.variant.product.title,
      price: toAmount(lineItem.variant.price),
      quantity: lineItem.quantity,
    })),
  });
});

// --- checkout_completed -> purchase -------------------------------------
// Payload: event.data.checkout (Shopify's Checkout type, with `order` set)
analytics.subscribe('checkout_completed', (event) => {
  const checkout = event.data.checkout;
  if (!checkout) return;

  pushEcommerce('purchase', {
    transaction_id: checkout.order ? checkout.order.id : checkout.token,
    currency: checkout.totalPrice.currencyCode,
    value: toAmount(checkout.totalPrice),
    items: (checkout.lineItems || []).map((lineItem) => ({
      item_id: lineItem.variant.id,
      item_name: lineItem.variant.product.title,
      price: toAmount(lineItem.variant.price),
      quantity: lineItem.quantity,
    })),
  });
});
