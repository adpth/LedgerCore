const view = document.querySelector("#view");
const connection = document.querySelector("#connection");
const disconnect = document.querySelector("#disconnect");
const labels = {overview: "Overview", journal: "Journal", accounts: "Accounts", reconciliation: "Reconciliation"};
const kinds = {deposit: "Deposit", withdrawal: "Withdrawal", transfer: "Transfer", reversal: "Reversal",
  payout_hold: "Payout hold", payout_post: "Payout posted", payout_release: "Payout released"};
const number = new Intl.NumberFormat();
let session;
let notice = "";
let revision = 0;
const filters = {kind: "", currency: ""};
let cursors = [null];

function node(tag, value, className) {
  const element = document.createElement(tag);
  if (value !== undefined) element.textContent = value;
  if (className) element.className = className;
  return element;
}

async function api(path) {
  const response = await fetch("/api/" + path);
  if (!response.ok) {
    const error = new Error(response.status === 401 ? "Your session expired. Sign in again." :
      response.status === 503 ? "Ledger data is temporarily unavailable." :
      "Could not load ledger data (" + response.status + ").");
    error.status = response.status;
    throw error;
  }
  return response;
}

async function json(path) { return (await api(path)).json(); }

function money(value, currency) {
  const amount = BigInt(value);
  const format = new Intl.NumberFormat(undefined, {style: "currency", currency, currencyDisplay: "code"});
  const digits = format.resolvedOptions().maximumFractionDigits;
  const scale = 10n ** BigInt(digits);
  const absolute = amount < 0n ? -amount : amount;
  const parts = format.formatToParts(amount < 0n ? -(absolute / scale) : absolute / scale);
  if (amount < 0n && absolute < scale) {
    const zero = format.formatToParts(-0);
    parts.splice(0, parts.length, ...zero);
  }
  return parts.map(part => part.type === "fraction" ?
    numberForFraction(absolute % scale, digits) : part.value).join("");
}

function numberForFraction(value, digits) {
  return new Intl.NumberFormat(undefined, {minimumIntegerDigits: digits, useGrouping: false}).format(value);
}

function heading(title, description) {
  const intro = node("div", undefined, "intro");
  intro.append(node("h1", title), node("p", description));
  return intro;
}

function panel(title) {
  const section = node("section", undefined, "panel");
  const header = node("div", undefined, "panel-title");
  header.append(node("h2", title));
  section.append(header);
  return [section, header];
}

function table(section, headers, rows) {
  if (!rows.length) {
    section.append(node("p", "No records to display.", "empty"));
    return;
  }
  const wrap = node("div", undefined, "table-wrap");
  wrap.tabIndex = 0;
  wrap.setAttribute("role", "region");
  wrap.setAttribute("aria-label", section.querySelector("h2").textContent);
  const grid = node("table");
  const head = grid.createTHead().insertRow();
  headers.forEach(title => {
    const cell = node("th", title);
    cell.scope = "col";
    if (title === "Amount" || title === "Balance") cell.className = "number";
    head.append(cell);
  });
  const body = grid.createTBody();
  rows.forEach(cells => {
    const row = body.insertRow();
    cells.forEach((value, index) => {
      const cell = row.insertCell();
      cell.append(value);
      if (headers[index] === "Amount" || headers[index] === "Balance") cell.className = "number";
    });
  });
  wrap.append(grid);
  section.append(wrap);
}

function journalTable(section, entries) {
  const expanders = [];
  table(section, ["Entry", "Type", "Reference", "Posted at", "Amount"], entries.map(entry => {
    const button = node("button", "#" + entry.id, "entry");
    button.type = "button";
    button.setAttribute("aria-expanded", "false");
    expanders.push([button, entry]);
    const total = entry.lines.reduce((sum, line) => {
      const amount = BigInt(line.amount_minor);
      return sum + (amount > 0n ? amount : 0n);
    }, 0n);
    return [button, node("span", kinds[entry.kind] || entry.kind, "kind"), entry.reference || "—",
      new Date(entry.created_at).toLocaleString(undefined, {dateStyle: "medium", timeStyle: "short"}),
      money(total, entry.currency)];
  }));
  expanders.forEach(([button, entry]) => {
    const detail = node("tr", undefined, "lines");
    detail.hidden = true;
    detail.id = "entry-lines-" + entry.id;
    button.setAttribute("aria-controls", detail.id);
    const cell = detail.insertCell();
    cell.colSpan = 5;
    const list = node("ul");
    entry.lines.forEach(line => list.append(node("li", line.account + " · " + money(line.amount_minor, entry.currency))));
    if (entry.reversal_of) list.append(node("li", "Reverses #" + entry.reversal_of));
    cell.append(list);
    button.closest("tr").after(detail);
    button.onclick = () => {
      detail.hidden = !detail.hidden;
      button.setAttribute("aria-expanded", String(!detail.hidden));
    };
  });
}

function selectFilter(label, options, field) {
  const select = node("select");
  select.setAttribute("aria-label", label);
  select.append(new Option(label, ""));
  options.forEach(([value, text]) => select.append(new Option(text, value)));
  select.value = filters[field];
  select.onchange = () => { filters[field] = select.value; cursors = [null]; render(); };
  return select;
}

const pages = {
  async overview() {
    const [stats, journal] = await Promise.all([json("overview"), json("entries?limit=6")]);
    const metrics = node("div", undefined, "metrics");
    const balances = Object.entries(stats.wallet_totals);
    const cards = [
      ["Wallet balances", balances.length ? balances.map(([currency, value]) => money(value, currency)) : ["—"]],
      ["Wallet accounts", [number.format(stats.wallets)]],
      ["Journal entries", [number.format(stats.entries)]],
      ["Pending events", [number.format(stats.pending_events)]],
    ];
    cards.forEach(([name, values]) => {
      const card = node("div", undefined, "metric");
      card.append(node("span", name), node("strong", values[0]));
      values.slice(1).forEach(value => card.append(node("small", value)));
      metrics.append(card);
    });
    const [section, header] = panel("Recent journal entries");
    const link = node("a", "View journal", "button");
    link.href = "#journal";
    header.append(link);
    journalTable(section, journal.items);
    return [heading("Ledger overview", "Wallet balances, posting activity and event delivery."), metrics, section];
  },
  async journal() {
    const params = new URLSearchParams(Object.entries(filters).filter(([, value]) => value));
    const query = new URLSearchParams(params);
    if (cursors.at(-1)) query.set("before", cursors.at(-1));
    const [data, accounts] = await Promise.all([json("entries?" + query), json("accounts")]);
    const [section, header] = panel("Posted entries");
    const exportButton = node("button", "Export CSV");
    header.append(exportButton);
    const exportStatus = node("p", undefined, "notice error");
    exportStatus.hidden = true;
    exportStatus.setAttribute("role", "status");
    exportButton.onclick = async () => {
      exportButton.disabled = true;
      exportStatus.hidden = true;
      const current = revision;
      try {
        const response = await api("export?" + params);
        const file = await response.blob();
        if (current !== revision) return;
        const url = URL.createObjectURL(file);
        const link = node("a");
        link.href = url;
        link.download = "journal.csv";
        link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      } catch (error) {
        if (current !== revision) return;
        if (error.status === 401) {
          session.authenticated = false;
          notice = error.message;
          render();
        } else {
          exportStatus.textContent = "Could not export the journal. Try again.";
          exportStatus.hidden = false;
        }
      } finally { exportButton.disabled = false; }
    };
    const controls = node("div", undefined, "controls");
    const currencies = [...new Set(accounts.map(account => account.currency))].sort();
    controls.append(selectFilter("All types", Object.entries(kinds), "kind"),
      selectFilter("All currencies", currencies.map(code => [code, code]), "currency"));
    section.append(controls);
    journalTable(section, data.items);
    section.append(exportStatus);
    if (data.next || cursors.length > 1) {
      const paging = node("div", undefined, "paging");
      const previous = node("button", "Previous");
      const next = node("button", "Next");
      previous.disabled = cursors.length === 1;
      next.disabled = !data.next;
      previous.onclick = () => { cursors.pop(); render(); };
      next.onclick = () => { cursors.push(data.next); render(); };
      paging.append(previous, next);
      section.append(paging);
    }
    return [heading("Journal", "Inspect the account lines behind each posted entry."), section];
  },
  async accounts() {
    const accounts = await json("accounts");
    const [section] = panel("Ledger accounts");
    table(section, ["Account", "ID", "Type", "Currency", "Balance"], accounts.map(account =>
      [account.name, "#" + account.id, account.kind, account.currency, money(account.balance_minor, account.currency)]));
    return [heading("Accounts", "Current balances by account and currency."), section];
  },
  async reconciliation() {
    const {mismatches} = await json("reconciliation");
    const [section, header] = panel(mismatches.length ? "Balance mismatches" : "All balances reconcile");
    const refresh = node("button", "Run check");
    refresh.onclick = render;
    header.append(refresh);
    if (mismatches.length) {
      table(section, ["Account", "Projected minor units", "Journal minor units"], mismatches.map(row =>
        ["#" + row.account_id, row.projected ?? "Missing", row.journal]));
    } else {
      section.append(node("p", "Every account balance matches the sum of its journal lines.", "notice"));
    }
    return [heading("Reconciliation", "Compare the balance projection with the journal."), section];
  },
};

function showRetry(message, retry) {
  const button = node("button", "Retry");
  button.onclick = retry;
  view.replaceChildren(node("p", message, "error"), button);
}

function login(message) {
  view.replaceChildren(document.querySelector("#login").content.cloneNode(true));
  const button = view.querySelector("#sign-in");
  const feedback = view.querySelector("#sign-in-feedback");
  const demo = session.mode === "demo";
  button.textContent = demo ? "Open demo workspace" : "Sign in with SSO";
  view.querySelector("#sign-in-description").textContent = demo ?
    "Explore a sample ledger with read-only finance access." :
    "Use your organization’s account to access the finance console.";
  feedback.textContent = session.enabled ? message : "Console sign-in is unavailable. Contact your administrator.";
  button.disabled = !session.enabled;
  button.onclick = async () => {
    if (!demo) { location.assign("/console/login"); return; }
    button.disabled = true;
    try {
      const response = await fetch("/console/demo", {method: "POST"});
      if (!response.ok) throw new Error();
      notice = "";
      await initialize();
    } catch {
      feedback.textContent = "The demo could not start. Try again.";
      button.disabled = false;
    }
  };
}

async function render() {
  const current = ++revision;
  const route = location.hash.slice(1);
  const page = Object.hasOwn(pages, route) ? route : "overview";
  document.querySelector("#breadcrumb").textContent = session?.authenticated ? labels[page] : "Sign in";
  document.querySelectorAll("nav a").forEach(link => {
    if (link.hash === "#" + page) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  document.querySelector("nav").hidden = !session?.authenticated;
  connection.textContent = session?.authenticated ?
    (session.mode === "demo" ? "Demo workspace" : "Signed in") : "Signed out";
  connection.classList.toggle("live", Boolean(session?.authenticated));
  disconnect.hidden = !session?.authenticated;
  if (!session) return;
  if (!session.authenticated) { login(notice); return; }
  view.replaceChildren(node("p", "Loading ledger…"));
  view.setAttribute("aria-busy", "true");
  try {
    const content = await pages[page]();
    if (current === revision) view.replaceChildren(...content);
  } catch (error) {
    if (current !== revision) return;
    if (error.status === 401) {
      session.authenticated = false;
      notice = error.message;
      render();
    } else {
      showRetry(error.status ? error.message : "Could not reach the ledger.", render);
    }
  } finally {
    if (current === revision || !session.authenticated) view.removeAttribute("aria-busy");
  }
}

async function initialize() {
  try {
    const response = await fetch("/console/session");
    if (!response.ok) throw new Error();
    session = await response.json();
    render();
  } catch {
    showRetry("Could not load the finance console.", initialize);
  }
}

disconnect.onclick = async () => {
  disconnect.disabled = true;
  const status = document.querySelector("#session-status");
  status.textContent = "";
  try {
    const response = await fetch("/console/logout", {method: "POST"});
    if (!response.ok) throw new Error();
    session.authenticated = false;
    notice = "You’ve signed out.";
    cursors = [null];
    render();
    document.querySelector("#sign-in")?.focus();
  } catch {
    status.textContent = "Could not sign out. Try again.";
  } finally {
    disconnect.disabled = false;
  }
};

const outcome = new URLSearchParams(location.search).get("sign_in");
if (outcome) {
  const messages = new Map([
    ["failed", "Sign-in could not be completed. Try again."],
    ["denied", "Your account doesn’t have finance access. Contact your administrator."],
    ["unavailable", "Console sign-in is unavailable. Contact your administrator."],
  ]);
  notice = messages.get(outcome) ?? "";
  history.replaceState(null, "", "/" + location.hash);
}
window.addEventListener("hashchange", () => { cursors = [null]; render(); });
initialize();
