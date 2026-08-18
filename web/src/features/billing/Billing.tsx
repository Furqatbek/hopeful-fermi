/**
 * What this centre has bought, and buying more.
 *
 * **Amounts are in tiyin everywhere on this screen.** 100 tiyin = 1 so'm, and
 * tiyin is what Click and Payme transact in — `payme.CheckPerformTransaction`
 * refuses a transaction whose `amount` does not equal `orders.amount_minor` to
 * the unit. So no amount is ever typed, converted or rounded here: the screen
 * buys a listed product at its listed price, sends the identifier the catalogue
 * gave it, and lets the server compute the total it will charge. `money.ts`
 * formats for reading and deliberately has no parser.
 *
 * **The order ends at the provider's door.** `POST /orders` answers with a
 * `redirect_url` and a `reference`; paying happens on Click's or Payme's own
 * page, and the money comes back through server-to-server callbacks that are
 * signed (Click) or Basic-authenticated (Payme). There is no in-page payment
 * flow to build and no card number to collect. The `reference` is the id on our
 * side that both providers echo back on every callback, and the field daily
 * reconciliation joins on, so it is shown prominently and is copyable — it is
 * what support and a bank dispute are answered with.
 *
 * **Buying a licence and covering a student are two different acts.** This
 * screen shows what the centre holds; `roster/Seats.tsx` shows which students
 * are covered by a seat licence. A seat bundle bought here is a quantity, and
 * every one of those seats still has to be given to a named student on the
 * Centre screen before `POST /assignments` will accept work for them.
 *
 * **What `GET /me/entitlements` lists is what the centre HOLDS, not what it may
 * do.** The handler filters on `revoked_at IS NULL` and nothing else — it does
 * not go through `billing.Entitlements.check()`, whatever the contract says — so
 * a lapsed or used-up grant is still in the list, and an org-held seat licence is
 * listed for a member who holds no seat. Both are the right answer to "what did
 * we buy" and the wrong answer to "what works right now", so each row carries the
 * status the dates and the balance actually imply.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";

import { api, problemText } from "../../api/client";
import { loadPrincipal } from "../../api/principal";
import type { components } from "../../api/schema";
import { formatPrice, lineTotal, parseQuantity } from "./money";

type Product = components["schemas"]["Product"];
type Price = NonNullable<Product["prices"]>[number];
type Order = components["schemas"]["Order"];

/** The two Uzbek rails this platform is integrated with, under the names their
 *  own checkout pages carry. `manual_bank` is in the contract's enum and is not
 *  offered: the redirect is built as `https://checkout.{provider}.uz/pay`, which
 *  for a bank transfer is not a page anyone can pay on. */
const PROVIDERS = [
  { value: "click", label: "Click" },
  { value: "payme", label: "Payme" },
] as const;

type ProviderValue = (typeof PROVIDERS)[number]["value"];

const KINDS: Record<string, string> = {
  seat_licence: "Seats for students",
  subscription: "Subscription",
  one_off: "One-off",
};

const SOURCES: Record<string, string> = {
  order: "bought",
  manual_grant: "granted by the platform",
  trial: "trial",
  seat: "seat licence",
  promo: "promotion",
};

export function Billing() {
  const queries = useQueryClient();
  const [orgChoice, setOrgChoice] = useState("");
  const [chosen, setChosen] = useState<{ product: Product; price: Price } | null>(null);
  const [quantityText, setQuantityText] = useState("1");
  const [provider, setProvider] = useState<ProviderValue>("click");
  /* Every order placed in this session, newest first. Nothing lists a centre's
     orders — there is no `GET /orders` — so once this page is closed the only
     handle on an unpaid order is the reference the admin wrote down. Keeping
     them all means a second purchase does not hide the first one's. */
  const [placed, setPlaced] = useState<{ order: Order; redirect: string }[]>([]);
  const [tracking, setTracking] = useState("");
  const [lookup, setLookup] = useState("");
  const [error, setError] = useState<string | null>(null);

  const orgs = useQuery({
    queryKey: ["orgs"],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/orgs", {
        params: { query: { limit: 25 } },
      });
      if (failure) throw failure;
      return data;
    },
  });

  // Read for its membership count alone. `GET /orgs` is the wrong source for
  // that: it lists every organization on the platform for a platform admin, who
  // is a member of none of them, while `/me/entitlements` returns org rows only
  // for centres the actor actually belongs to. The two numbers are different and
  // the label below depends on the second.
  const principal = useQuery({
    queryKey: ["principal"],
    queryFn: loadPrincipal,
    staleTime: Infinity,
  });

  const products = useQuery({
    queryKey: ["products"],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/products");
      if (failure) throw failure;
      return data;
    },
  });

  const entitlements = useQuery({
    queryKey: ["entitlements"],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/me/entitlements");
      if (failure) throw failure;
      return data;
    },
  });

  const order = useQuery({
    queryKey: ["order", tracking],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/orders/{xid}", {
        params: { path: { xid: tracking } },
      });
      if (failure) throw failure;
      return data;
    },
    enabled: Boolean(tracking),
    // An order becomes `paid` when the provider calls us back, which happens
    // while the admin is on the provider's page and not on this one. Refetching
    // on focus is how the status is current when they come back to this tab.
    refetchOnWindowFocus: true,
  });

  const items = orgs.data?.items ?? [];
  const orgXid = orgChoice || items[0]?.xid || "";
  const orgName = items.find((o) => o.xid === orgXid)?.name ?? "";
  const centres = principal.data?.memberships?.length ?? 0;

  const quantity = parseQuantity(quantityText);
  const unit = chosen?.price.amount_minor;
  const total = typeof unit === "number" && quantity !== null
    ? lineTotal(unit, quantity)
    : null;

  /**
   * One key per distinct order, held until that order exists.
   *
   * The contract declares `Idempotency-Key` required on this endpoint and the
   * reason is a double-tapped Buy button: two orders, one of which the admin
   * will never pay and which then sits in the expiry sweeper. A fresh key per
   * click would defeat that entirely.
   *
   * Keyed on the body, because the server answers 409 `idempotency_key_reused`
   * when one key arrives with two different bodies — so editing the quantity has
   * to change the key. Dropped once the order is created, so buying the same
   * bundle again next term is a second order rather than a replay of the first.
   */
  const keys = useRef(new Map<string, string>());

  const buy = useMutation({
    mutationFn: async () => {
      if (!chosen) throw new Error("Choose what to buy.");
      if (quantity === null) throw new Error("How many is not a whole number.");
      // The catalogue's own value, forwarded exactly as it arrived. The contract
      // declares this a uuid and the server sends and expects the price row's
      // integer id, so it is opaque: reading it, reformatting it, or rebuilding
      // it from a form field is how it stops matching the row it names.
      const priceXid = chosen.price.xid;
      if (priceXid === undefined) throw new Error("This price has no identifier.");

      const body = {
        price_xid: priceXid,
        quantity,
        provider,
        ...(orgXid ? { org_xid: orgXid } : {}),
        // Stored on the order. Today's `redirect_url` is built from the provider
        // name alone and carries nothing back, so this is a record of where the
        // buyer started rather than a working return trip.
        return_url: window.location.href,
      };
      const signature = JSON.stringify(body);
      let key = keys.current.get(signature);
      if (key === undefined) {
        key = crypto.randomUUID();
        keys.current.set(signature, key);
      }

      const { data, error: failure } = await api.POST("/orders", {
        params: { header: { "Idempotency-Key": key } },
        body,
      });
      if (failure) throw failure;
      keys.current.delete(signature);
      return data;
    },
    onSuccess: (data) => {
      setError(null);
      if (data?.order) {
        setPlaced((previous) => [
          { order: data.order, redirect: data.redirect_url },
          ...previous,
        ]);
        if (data.order.xid) setTracking(data.order.xid);
      }
      // The licence is recorded against the centre separately from the payment,
      // so this is asked again rather than assumed to have changed.
      void queries.invalidateQueries({ queryKey: ["entitlements"] });
    },
    onError: (failure) => setError(problemText(failure) || String(failure)),
  });

  return (
    <div className="page">
      <h1>Billing</h1>
      {error && <p className="error">{error}</p>}

      <h2>What this centre holds</h2>
      {entitlements.isError && (
        <p className="error">{problemText(entitlements.error)}</p>
      )}
      {entitlements.data?.length === 0 && (
        <p className="muted">
          Nothing is recorded against you or your centre yet. Until a licence is
          here, setting a mock for a student is refused.
        </p>
      )}
      {entitlements.data && entitlements.data.length > 0 && (
        <>
          <div className="scroll">
            <table>
              <thead>
                <tr>
                  <th>What</th>
                  <th>Held by</th>
                  <th>How</th>
                  <th>Left</th>
                  <th>Runs to</th>
                  <th>Now</th>
                </tr>
              </thead>
              <tbody>
                {entitlements.data.map((row, index) => (
                  <tr key={`${row.feature}-${row.starts_at}-${index}`}>
                    <td>{row.feature}</td>
                    <td className="muted">
                      {row.subject_kind === "org"
                        /* The response says the row belongs to an organization the
                           actor is in, and does not say WHICH — there is no org
                           field on it at all. With more than one membership this
                           screen must not name a centre it cannot identify. */
                        ? (centres > 1 ? "A centre you belong to" : "Your centre")
                        : "You"}
                    </td>
                    <td className="muted">{SOURCES[row.source_kind ?? ""] ?? row.source_kind}</td>
                    <td className="num">
                      {row.quantity === null || row.quantity === undefined
                        ? "unlimited"
                        : `${row.remaining ?? 0} of ${row.quantity}`}
                    </td>
                    <td className="muted">
                      {row.expires_at
                        ? new Date(row.expires_at).toLocaleDateString()
                        : "no end date"}
                    </td>
                    <td>{standing(row)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="muted">
            This is what has been bought, not what will be allowed: a grant that
            has run out or lapsed is still listed, with its state in the last
            column. A seat licence covers only the students who have been given
            one of its seats — that is done on the Centre screen, and a seat that
            is not given to anybody covers nobody.
          </p>
        </>
      )}

      <h2>Catalogue</h2>
      {products.isError && <p className="error">{problemText(products.error)}</p>}
      {products.data?.length === 0 && (
        <p className="muted">Nothing is on sale at the moment.</p>
      )}
      <div className="scroll">
        <table>
          <thead>
            <tr><th>Product</th><th>Price</th><th /></tr>
          </thead>
          <tbody>
            {products.data?.flatMap((product) => {
              const prices = product.prices ?? [];
              if (prices.length === 0) {
                return [(
                  <tr key={product.xid}>
                    <td>
                      {product.name}
                      <div className="muted">{KINDS[product.kind ?? ""] ?? product.kind}</div>
                    </td>
                    {/* An active product whose prices are all inactive. The
                        catalogue query left-joins prices on `active`, so this is
                        an empty list rather than a missing row, and there is
                        nothing to send as `price_xid`. */}
                    <td className="muted" colSpan={2}>No price set.</td>
                  </tr>
                )];
              }
              return prices.map((price, index) => (
                <tr key={`${product.xid}-${index}`}>
                  <td>
                    {index === 0 && (
                      <>
                        {product.name}
                        <div className="muted">
                          {KINDS[product.kind ?? ""] ?? product.kind}
                          {product.description ? ` · ${product.description}` : ""}
                        </div>
                      </>
                    )}
                  </td>
                  <td className="num">
                    {typeof price.amount_minor === "number"
                      ? formatPrice(price.amount_minor, price.currency)
                      : "—"}
                    {price.interval && (
                      <span className="muted"> per {price.interval}</span>
                    )}
                  </td>
                  <td>
                    <button
                      className="link"
                      onClick={() => {
                        setError(null);
                        setChosen({ product, price });
                      }}
                    >
                      Buy this
                    </button>
                  </td>
                </tr>
              ));
            })}
          </tbody>
        </table>
      </div>

      {chosen && (
        <>
          <h2>Buy: {chosen.product.name}</h2>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              setError(null);
              buy.mutate();
            }}
          >
            <label htmlFor="b-org">Billed to</label>
            {/* Shown even with one centre, and required. An order carrying no
                `org_xid` is booked to the person who placed it, and a centre's
                invoice has to survive the admin who placed it leaving. */}
            <select
              id="b-org"
              value={orgXid}
              onChange={(event) => setOrgChoice(event.target.value)}
              required
            >
              <option value="">— choose a centre —</option>
              {items.map((org) => (
                <option key={org.xid} value={org.xid}>{org.name}</option>
              ))}
            </select>

            <label htmlFor="b-qty">How many</label>
            <input
              id="b-qty"
              value={quantityText}
              onChange={(event) => setQuantityText(event.target.value)}
              inputMode="numeric"
              required
            />
            {quantity === null && (
              <p className="error">
                Enter a whole number of one or more.
              </p>
            )}

            <label htmlFor="b-provider">Pay with</label>
            <select
              id="b-provider"
              value={provider}
              onChange={(event) => setProvider(event.target.value as ProviderValue)}
            >
              {PROVIDERS.map((option) => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </select>

            <p>
              Total{" "}
              <strong className="num">
                {total === null
                  ? "—"
                  : formatPrice(total, chosen.price.currency)}
              </strong>
              <span className="muted">
                {" "}— the server works the charge out again from the listed price,
                and its figure is the one you pay.
              </span>
            </p>

            <button disabled={buy.isPending || quantity === null || !orgXid}>
              {buy.isPending ? "Placing…" : "Place the order"}
            </button>
          </form>
          <p className="muted">
            Placing an order does not pay for it. You are handed to{" "}
            {PROVIDERS.find((p) => p.value === provider)?.label} to pay, and the
            order stays unpaid until they tell us otherwise.
            {orgName && ` This order is billed to ${orgName}.`}
          </p>
        </>
      )}

      {placed.map(({ order: row, redirect }) => (
        <div className="issued" key={row.xid}>
          <h2>Order placed</h2>
          <p>
            <strong className="num">
              {typeof row.amount_minor === "number"
                ? formatPrice(row.amount_minor, row.currency ?? "UZS")
                : "—"}
            </strong>{" "}
            · {PROVIDERS.find((p) => p.value === row.provider)?.label ?? row.provider}{" "}
            · {row.status}
          </p>
          <p>
            <strong>Keep this reference.</strong> Both providers echo it back on
            every callback, and it is what a payment question is answered with.
          </p>
          <code className="token">{row.reference}</code>
          <div className="row">
            <button
              type="button"
              onClick={() => {
                if (row.reference) void navigator.clipboard?.writeText(row.reference);
              }}
            >
              Copy the reference
            </button>
            <a
              className="link"
              href={redirect}
              /* A new tab, deliberately. Nothing lists a centre's orders, so this
                 page holds the only copy of the reference and the order id until
                 the order is paid — navigating away in this tab loses both. */
              target="_blank"
              rel="noreferrer"
            >
              Pay at {PROVIDERS.find((p) => p.value === row.provider)?.label
                      ?? row.provider}
            </a>
            <button
              type="button"
              className="link"
              onClick={() => { if (row.xid) setTracking(row.xid); }}
            >
              Check the status
            </button>
          </div>
          <p className="muted">
            Order id <code>{row.xid}</code>. Nothing here lists past orders, so
            note it down if you need to check this one after closing the console.
          </p>
        </div>
      ))}

      <h2>Check an order</h2>
      <form
        className="row"
        onSubmit={(event) => {
          event.preventDefault();
          setError(null);
          setTracking(lookup.trim());
        }}
      >
        <input
          value={lookup}
          onChange={(event) => setLookup(event.target.value)}
          placeholder="Order id"
          aria-label="Order id"
        />
        <button disabled={!lookup.trim()}>Check</button>
      </form>
      {order.isError && <p className="error">{problemText(order.error)}</p>}
      {order.data && (
        <div className="scroll">
          <table>
            <tbody>
              <tr>
                <td>Reference</td>
                <td><code>{order.data.reference}</code></td>
              </tr>
              <tr>
                <td>Status</td>
                <td>
                  <strong>{order.data.status}</strong>
                  {order.data.status === "awaiting_payment" && (
                    <span className="muted">
                      {" "}— we have not heard from the provider yet
                    </span>
                  )}
                </td>
              </tr>
              <tr>
                <td>Amount</td>
                <td className="num">
                  {typeof order.data.amount_minor === "number"
                    ? formatPrice(order.data.amount_minor, order.data.currency ?? "UZS")
                    : "—"}
                </td>
              </tr>
              <tr>
                {/* Named, because chasing a payment means contacting the right
                    company and the reference is the only thing they will look it
                    up by. */}
                <td>Paid with</td>
                <td className="muted">
                  {PROVIDERS.find((p) => p.value === order.data.provider)?.label
                   ?? order.data.provider}
                </td>
              </tr>
              <tr>
                <td>Paid</td>
                <td className="muted">
                  {order.data.paid_at
                    ? new Date(order.data.paid_at).toLocaleString()
                    : "not yet"}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      )}
      <p className="muted">
        A paid order and a live licence are two records. If an order is paid and
        the licence above has not appeared, send the reference to the platform —
        do not buy it a second time.
      </p>
    </div>
  );
}

/**
 * What a listed grant actually amounts to today.
 *
 * `GET /me/entitlements` drops revoked rows and nothing else, so the list is a
 * purchase history rather than a permission set. These four states are the ones
 * `Entitlements.check()` would answer with for the same row, worked out from the
 * fields the response does carry — a lapsed licence that still reads "Live"
 * because it is in the list is the specific confusion this prevents.
 */
function standing(row: components["schemas"]["Entitlement"]): string {
  const now = Date.now();
  if (row.starts_at && new Date(row.starts_at).getTime() > now) return "Not started";
  if (row.expires_at && new Date(row.expires_at).getTime() <= now) return "Expired";
  if (row.quantity !== null && row.quantity !== undefined && (row.remaining ?? 0) <= 0) {
    return "Used up";
  }
  return "Live";
}
