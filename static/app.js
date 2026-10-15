"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const state = { products: [], filter: "all", search: "", editing: null, deleting: null, busy: new Set(), refreshing: false };
let toastTimer;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { "Content-Type": "application/json", ...options.headers } });
  let data = null;
  if (response.status !== 204) {
    try { data = await response.json(); } catch { /* Display the HTTP status for non-JSON errors. */ }
  }
  if (!response.ok) {
    const detail = typeof data?.detail === "string" ? data.detail : Array.isArray(data?.detail) ? data.detail.map(item => item.msg).join("；") : "请求失败，请稍后重试";
    throw new Error(`${detail}（${response.status}）`);
  }
  return data;
}

function money(value, currency = "CNY") {
  if (value === null || value === undefined || value === "" || !Number.isFinite(Number(value))) return "—";
  try { return new Intl.NumberFormat("zh-CN", { style: "currency", currency, minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(Number(value)); }
  catch { return `${currency} ${Number(value).toFixed(2)}`; }
}

function dateText(value) {
  if (!value) return "尚未检查";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "尚未检查";
  return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
}

function hostname(value) { try { return new URL(value).hostname.replace(/^www\./, ""); } catch { return "商品网站"; } }
function intervalText(minutes) { return minutes < 60 ? `${minutes} 分钟` : minutes % 60 === 0 ? `${minutes / 60} 小时` : `${minutes} 分钟`; }
function toast(message, error = false) {
  const node = $("#toast");
  clearTimeout(toastTimer);
  node.textContent = message;
  node.classList.toggle("error", error);
  node.hidden = false;
  toastTimer = setTimeout(() => { node.hidden = true; }, error ? 6500 : 3800);
}

function showPageError(message) {
  $("#page-error").textContent = message || "";
  $("#page-error").hidden = !message;
}

async function loadProducts(showError = true) {
  try {
    state.products = await api("/api/products");
    showPageError("");
    render();
    return true;
  } catch (error) {
    if (showError) showPageError(`无法载入监测清单：${error.message}`);
    if (!state.products.length) $("#product-list").replaceChildren(el("div", "loading-state", "暂时无法连接服务，请稍后刷新页面。"));
    return false;
  }
}

function render() {
  const products = state.products;
  $("#stat-total").textContent = products.length;
  $("#stat-active").textContent = products.filter(product => product.enabled).length;
  $("#stat-alert").textContent = products.filter(product => product.alert).length;
  $("#nav-count").textContent = products.length;
  $("#list-count").textContent = products.length;
  $("#refresh-all").disabled = state.refreshing || !products.some(product => product.enabled);
  const visible = products.filter(product => {
    const matchesFilter = state.filter === "all" || (state.filter === "active" && product.enabled) || (state.filter === "paused" && !product.enabled) || (state.filter === "alert" && product.alert);
    return matchesFilter && `${product.name || ""} ${product.url}`.toLocaleLowerCase().includes(state.search);
  });
  const list = $("#product-list");
  list.replaceChildren();
  if (!visible.length) {
    const empty = el("div", "empty-state");
    const art = el("div", "empty-art");
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 64 64");
    svg.setAttribute("fill", "none");
    svg.setAttribute("aria-hidden", "true");
    const shape = document.createElementNS(svg.namespaceURI, "path");
    shape.setAttribute("d", "M14 21h36v32H14z M23 23v-7a9 9 0 0 1 18 0v7 M23 39l7 5 12-14");
    shape.setAttribute("stroke", "currentColor");
    shape.setAttribute("stroke-width", "1.8");
    shape.setAttribute("stroke-linecap", "round");
    shape.setAttribute("stroke-linejoin", "round");
    svg.append(shape); art.append(svg); empty.append(art);
    empty.append(el("h3", "", products.length ? "没有找到匹配的商品" : "好价格，从第一件商品开始"));
    empty.append(el("p", "", products.length ? "试试其他关键词，或切换筛选条件。" : "添加商品链接，记录价格走势。设置目标价后，达到理想价格的商品会在这里标记。"));
    const button = el("button", "button primary", products.length ? "查看全部商品" : "＋ 添加第一件商品");
    button.addEventListener("click", () => {
      if (!products.length) openProduct();
      else { state.filter = "all"; state.search = ""; $("#search").value = ""; updateFilters(); render(); }
    });
    empty.append(button); list.append(empty);
  } else visible.forEach(product => list.append(productCard(product)));
}

function makeAction(label, symbol, onClick, className = "") {
  const button = el("button", `action-button ${className}`, symbol);
  button.type = "button";
  button.title = label;
  button.setAttribute("aria-label", label);
  button.addEventListener("click", onClick);
  return button;
}

function productCard(product) {
  const card = el("article", `product-card${product.alert ? " alert-card" : ""}`);
  const heading = el("div", "card-heading");
  const host = hostname(product.url);
  heading.append(el("div", "store-icon", host.slice(0, 1)));
  const name = el("div", "card-name");
  name.append(el("h3", "", product.name || host));
  const link = el("a", "", `${host} ↗`);
  try { const url = new URL(product.url); if (["http:", "https:"].includes(url.protocol)) link.href = url.href; } catch { /* Invalid stored URLs are not linked. */ }
  link.target = "_blank"; link.rel = "noopener noreferrer"; link.title = product.url;
  name.append(link); heading.append(name);
  const status = !product.enabled ? ["paused", "已暂停"] : product.error ? ["failed", "检查异常"] : product.alert ? ["reached", "已达目标"] : ["", "监测中"];
  heading.append(el("span", `status-badge ${status[0]}`, status[1]));
  card.append(heading);
  const prices = el("div", "price-row");
  const current = el("div", "current-price");
  current.append(el("div", "price-label", "最近采集价"));
  const value = el("div", `price-value${product.latest_price === null ? " empty" : ""}`, money(product.latest_price, product.currency));
  current.append(value);
  if (product.latest_price !== null && product.previous_price !== null && Number(product.previous_price) > 0) {
    const difference = Number(product.latest_price) - Number(product.previous_price);
    const percent = Math.abs(difference / Number(product.previous_price) * 100).toFixed(1);
    current.append(el("span", `price-delta${difference > 0 ? " up" : ""}`, difference === 0 ? "较上次价格持平" : `${difference < 0 ? "↓" : "↑"} ${percent}% 较上次`));
  } else current.append(el("span", "price-delta", product.latest_price === null ? "等待首次成功采集" : "首次采集，等待后续价格"));
  const target = el("div", "target");
  target.append(el("div", "price-label", "目标价格"), el("div", "target-value", product.target_price ? money(product.target_price, product.currency) : "未设置"));
  prices.append(current, target); card.append(prices);
  const chartArea = el("div", "chart-area");
  const history = (product.history || []).filter(point => point.currency === product.currency && Number.isFinite(Number(point.price)));
  chartArea.append(history.length > 1 ? sparkline(history) : el("div", "chart-empty", history.length ? "累积更多记录后显示价格趋势" : "价格记录将在这里慢慢累积"));
  const caption = el("div", "chart-caption");
  caption.append(el("span", "", "价格趋势"), el("span", "", `${history.length} 条记录 · ${product.currency}`));
  chartArea.append(caption); card.append(chartArea);
  if (product.error) card.append(el("p", "product-error", product.error));
  const footer = el("div", "card-footer");
  const checked = el("div", "checked-time");
  checked.append(el("div", "", `最近检查 ${dateText(product.last_checked)}`), el("div", "", `每 ${intervalText(product.interval_minutes)}检查`));
  const actions = el("div", "card-actions");
  actions.append(
    makeAction("立即检查价格", "↻", () => checkProduct(product)),
    makeAction(product.enabled ? "暂停监测" : "恢复监测", product.enabled ? "Ⅱ" : "▷", () => toggleProduct(product)),
    makeAction("编辑监测设置", "✎", () => openProduct(product)),
    makeAction("删除商品", "×", () => openDelete(product), "delete")
  );
  if (state.busy.has(product.id)) $$("button", actions).forEach(button => { button.disabled = true; });
  footer.append(checked, actions); card.append(footer);
  return card;
}

function sparkline(history) {
  const namespace = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(namespace, "svg");
  svg.classList.add("sparkline"); svg.setAttribute("viewBox", "0 0 350 60"); svg.setAttribute("preserveAspectRatio", "none"); svg.setAttribute("role", "img");
  const prices = history.map(point => Number(point.price));
  const min = Math.min(...prices); const max = Math.max(...prices); const range = max - min || 1;
  const points = prices.map((price, index) => [4 + index / (prices.length - 1) * 342, 48 - (price - min) / range * 36]);
  const title = document.createElementNS(namespace, "title"); title.textContent = `价格趋势，共 ${prices.length} 次记录，最低 ${min}，最高 ${max}`; svg.append(title);
  const fill = document.createElementNS(namespace, "path"); fill.setAttribute("d", `M${points.map(point => point.join(",")).join(" L")} L346,59 L4,59 Z`); fill.setAttribute("fill", "#f0f5e8");
  const line = document.createElementNS(namespace, "polyline"); line.setAttribute("points", points.map(point => point.join(",")).join(" ")); line.setAttribute("fill", "none"); line.setAttribute("stroke", "#8aa76b"); line.setAttribute("stroke-width", "1.6"); line.setAttribute("stroke-linecap", "round"); line.setAttribute("stroke-linejoin", "round");
  svg.append(fill, line); return svg;
}

function updateFilters() {
  $$("[data-filter]").forEach(button => { const selected = button.dataset.filter === state.filter; button.classList.toggle("selected", selected); button.setAttribute("aria-pressed", String(selected)); });
}

function openProduct(product = null) {
  state.editing = product;
  const form = $("#product-form");
  form.reset();
  $("#form-error").hidden = true;
  $("#currency-hint").hidden = true;
  form.elements.url.disabled = Boolean(product);
  form.elements.url.value = product?.url || "";
  form.elements.name.value = product?.name || "";
  form.elements.currency.value = product?.currency || "CNY";
  if (!form.elements.currency.value && product?.currency) { const option = el("option", "", product.currency); option.value = product.currency; form.elements.currency.append(option); form.elements.currency.value = product.currency; }
  form.elements.target_price.value = product?.target_price || "";
  form.elements.selector.value = product?.selector || "";
  form.elements.interval_minutes.value = product?.interval_minutes || 60;
  if (!form.elements.interval_minutes.value && product?.interval_minutes) { const option = el("option", "", `每 ${intervalText(product.interval_minutes)}`); option.value = product.interval_minutes; form.elements.interval_minutes.append(option); form.elements.interval_minutes.value = product.interval_minutes; }
  $("#dialog-title").textContent = product ? "编辑监测设置" : "添加监测商品";
  $("#save-product").textContent = product ? "保存设置" : "开始监测";
  $("#product-dialog").showModal();
  if (product) form.elements.name.focus();
}

async function saveProduct(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = $("#save-product");
  const editing = state.editing;
  const body = {
    name: form.elements.name.value.trim(),
    currency: form.elements.currency.value,
    target_price: form.elements.target_price.value.trim() || null,
    selector: form.elements.selector.value.trim(),
    interval_minutes: Number(form.elements.interval_minutes.value)
  };
  if (!editing) body.url = form.elements.url.value.trim();
  button.disabled = true;
  button.textContent = "正在保存…";
  $("#form-error").hidden = true;
  try {
    await api(editing ? `/api/products/${editing.id}` : "/api/products", { method: editing ? "PATCH" : "POST", body: JSON.stringify(body) });
    $("#product-dialog").close();
    await loadProducts();
    toast(editing ? "监测设置已保存" : "已添加商品，将按设定频率检查价格");
  } catch (error) { $("#form-error").textContent = error.message; $("#form-error").hidden = false; }
  finally { button.disabled = false; button.textContent = editing ? "保存设置" : "开始监测"; }
}

async function checkProduct(product) {
  if (state.busy.has(product.id)) return;
  state.busy.add(product.id); render();
  try {
    const result = await api(`/api/products/${product.id}/check`, { method: "POST" });
    await loadProducts();
    const updated = state.products.find(item => item.id === product.id) || result;
    if (updated?.error) toast(`检查未成功：${updated.error}`, true);
    else toast("已完成价格检查");
  } catch (error) { toast(error.message, true); }
  finally { state.busy.delete(product.id); render(); }
}

async function toggleProduct(product) {
  state.busy.add(product.id); render();
  try { await api(`/api/products/${product.id}`, { method: "PATCH", body: JSON.stringify({ enabled: !product.enabled }) }); await loadProducts(); toast(product.enabled ? "已暂停自动监测" : "已恢复自动监测"); }
  catch (error) { toast(error.message, true); }
  finally { state.busy.delete(product.id); render(); }
}

function openDelete(product) {
  state.deleting = product;
  $("#delete-description").textContent = product.name || hostname(product.url);
  $("#delete-error").hidden = true;
  $("#delete-dialog").showModal();
}

async function deleteProduct() {
  if (!state.deleting) return;
  const button = $("#confirm-delete"); button.disabled = true;
  try { await api(`/api/products/${state.deleting.id}`, { method: "DELETE" }); $("#delete-dialog").close(); state.deleting = null; await loadProducts(); toast("商品及监测记录已删除"); }
  catch (error) { $("#delete-error").textContent = error.message; $("#delete-error").hidden = false; }
  finally { button.disabled = false; }
}

async function checkAll() {
  state.refreshing = true;
  $("#refresh-all").textContent = "正在检查…"; render();
  try {
    await api("/api/check-all", { method: "POST" });
    await loadProducts();
    const failed = state.products.filter(product => product.enabled && product.error).length;
    toast(failed ? `已完成检查，${failed} 件商品存在读取异常，请查看商品说明` : "全部在监测商品已完成价格检查", Boolean(failed));
  } catch (error) { toast(error.message, true); }
  finally { state.refreshing = false; $("#refresh-all").textContent = "↻ 检查全部"; render(); }
}

async function loadReference() {
  try {
    const reference = await api("/api/build-reference");
    if (!reference?.name) return;
    const panel = $("#reference-section");
    const top = el("div", "reference-top");
    const info = el("div", "reference-info");
    info.append(el("div", "reference-kicker", "YOUR BUILD · 装机参考"));
    const title = el("h2", "reference-title", reference.name); title.id = "reference-title";
    info.append(title);
    const meta = el("div", "reference-meta", `${reference.retailer || "用户提供"} · 商品链接待补充`);
    meta.append(el("span", "reference-badge", reference.verified_live ? "已联网核验" : "用户提供 · 未联网核验"));
    info.append(meta);
    const price = el("div", "reference-price"); price.append(el("strong", "", money(reference.reference_price, reference.currency).replace(/\.00$/, "")), el("small", "", "折后参考报价"));
    top.append(info, price); panel.append(top);
    const details = el("details", "reference-details"); details.append(el("summary", "", "查看整机配置与报价说明"));
    const list = el("div", "component-list");
    (reference.components || []).forEach(component => {
      const row = el("div", "component"); const description = el("div", "");
      description.append(el("strong", "", component.name));
      if (component.details) description.append(el("small", "", component.details));
      if (component.upgrade) description.append(el("small", "", component.upgrade));
      row.append(el("span", "", component.category), description); list.append(row);
    });
    details.append(list);
    const notes = el("ul", "reference-notes");
    (reference.notes || []).forEach(note => notes.append(el("li", "", note)));
    details.append(notes); panel.append(details); panel.hidden = false;
  } catch { /* The optional reference never blocks the monitoring list. */ }
}

$("#add-product").addEventListener("click", () => openProduct());
$("#product-form").addEventListener("submit", saveProduct);
$("#product-form").elements.currency.addEventListener("change", () => { $("#product-form").elements.target_price.value = ""; $("#currency-hint").hidden = false; });
$("#confirm-delete").addEventListener("click", deleteProduct);
$("#refresh-all").addEventListener("click", checkAll);
$("#search").addEventListener("input", event => { state.search = event.target.value.trim().toLocaleLowerCase(); render(); });
$$('[data-close]').forEach(button => button.addEventListener("click", () => { if (!$("#save-product").disabled && !$("#confirm-delete").disabled) document.getElementById(button.dataset.close).close(); }));
$$('[data-filter]').forEach(button => button.addEventListener("click", () => { state.filter = button.dataset.filter; updateFilters(); render(); }));
$$('dialog').forEach(dialog => dialog.addEventListener("cancel", event => { if ($("#save-product").disabled || $("#confirm-delete").disabled) event.preventDefault(); }));

Promise.allSettled([
  loadProducts(),
  loadReference(),
  api("/api/health").then(() => { $("#service-text").textContent = "监测服务已连接"; }).catch(() => { $("#service-text").textContent = "监测服务连接失败"; })
]);
setInterval(() => { if (!document.hidden && !$("dialog[open]")) loadProducts(false); }, 60000);
