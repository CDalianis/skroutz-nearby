const panel = document.getElementById("panel");
const queryInput = document.getElementById("query");
const locationLabel = document.getElementById("location-label");
const placeForm = document.getElementById("place-form");
const placeInput = document.getElementById("place-input");
const placeMatches = document.getElementById("place-matches");
const mapNote = document.getElementById("map-note");

const state = {
  user: null,
  query: "",
  products: [],
  nextPage: null,
  total: 0,
  filters: { sort: [], groups: [] },
  product: null,
  places: [],
  radius: "15",
  storeSort: "distance",
  picking: false,
  booted: false,
  overview: [],
};

let map;
let userLayer;
let overviewLayer;
let storeLayer;
let searchToken = 0;

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[ch]));
}

function kmBetween(a, b) {
  const toRad = (n) => (n * Math.PI) / 180;
  const dLat = toRad(b.lat - a.lat);
  const dLng = toRad(b.lng - a.lng);
  const lat1 = toRad(a.lat);
  const lat2 = toRad(b.lat);
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLng / 2) ** 2;
  return 6371 * 2 * Math.asin(Math.min(1, Math.sqrt(h)));
}

function formatKm(km) {
  if (km == null || Number.isNaN(km)) return "";
  if (km < 1) return `${Math.round(km * 1000)} m`;
  if (km < 10) return `${km.toFixed(1)} km`;
  return `${Math.round(km)} km`;
}

function sourceText(source) {
  if (source === "gps") return "from this PC";
  if (source === "network") return "from your network";
  if (source === "typed") return "place you set";
  if (source === "saved") return "saved on this PC";
  return "";
}

function bridge() {
  if (!window.pywebview || !window.pywebview.api) {
    throw new Error("The desktop app is still starting.");
  }
  return window.pywebview.api;
}

function initMap() {
  map = L.map("map", { zoomControl: true, attributionControl: true }).setView([38.2, 23.8], 6);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  }).addTo(map);
  userLayer = L.layerGroup().addTo(map);
  overviewLayer = L.layerGroup().addTo(map);
  storeLayer = L.layerGroup().addTo(map);
  map.on("click", (event) => {
    if (!state.picking) return;
    pinFromMap(event.latlng.lat, event.latlng.lng);
  });
  setTimeout(() => map.invalidateSize(), 200);
}

function setUser(place) {
  state.user = place;
  const via = sourceText(place.source);
  locationLabel.textContent = via ? `${place.label} · ${via}` : place.label;
  userLayer.clearLayers();
  const icon = L.divIcon({
    className: "",
    html: '<div class="you-pin"><span></span></div>',
    iconSize: [28, 28],
    iconAnchor: [14, 14],
  });
  L.marker([place.lat, place.lng], { icon, zIndexOffset: 1000 })
    .addTo(userLayer)
    .bindPopup(`<h3>You</h3><p>${esc(place.label)}</p>`);
  if (!state.product) {
    storeLayer.clearLayers();
    map.setView([place.lat, place.lng], 14);
    if (state.overview.length) drawOverview();
    else {
      mapNote.hidden = false;
      mapNote.textContent = `Map is on ${place.label}. Search a product to see stores around this address.`;
    }
  } else {
    annotateDistances();
    const slot = document.querySelector(".product-block.open .stores");
    if (slot) renderStores(slot);
  }
}

let pinToken = 0;

function startPicking() {
  state.picking = true;
  state.noteBeforePick = { hidden: mapNote.hidden, text: mapNote.textContent };
  placeForm.hidden = false;
  document.getElementById("place-btn").classList.add("active");
  map.getContainer().classList.add("picking");
  mapNote.hidden = false;
  mapNote.textContent = "Click the map to set your location, or type an address.";
  placeInput.focus();
}

function stopPicking(restoreNote) {
  pinToken += 1;
  state.picking = false;
  placeForm.hidden = true;
  placeMatches.hidden = true;
  placeMatches.innerHTML = "";
  placeInput.value = "";
  document.getElementById("place-btn").classList.remove("active");
  if (map) map.getContainer().classList.remove("picking");
  if (restoreNote && state.noteBeforePick) {
    mapNote.hidden = state.noteBeforePick.hidden;
    mapNote.textContent = state.noteBeforePick.text;
  }
}

async function pinFromMap(lat, lng) {
  const token = ++pinToken;
  locationLabel.textContent = "Setting that spot…";
  const saved = await bridge().pin_location(lat, lng);
  if (token !== pinToken) return;
  if (!saved.ok) {
    locationLabel.textContent = saved.error || "Could not use that spot.";
    return;
  }
  setUser(saved);
  stopPicking(false);
}

async function choosePlace(match) {
  const saved = await bridge().remember_location(match.lat, match.lng, match.label);
  if (!saved.ok) {
    locationLabel.textContent = saved.error || "Could not use that address.";
    return;
  }
  setUser(saved);
  stopPicking(false);
}

function showMessage(title, body) {
  panel.innerHTML = `<div class="message"><strong>${esc(title)}</strong>${esc(body)}</div>`;
}

function welcome() {
  panel.innerHTML = `<div class="message"><strong>Search Skroutz</strong>Type a product, then open it to sort the stores by distance, price, or shop rating.</div>`;
}

async function boot() {
  if (state.booted) return;
  state.booted = true;
  initMap();
  welcome();
  queryInput.focus();
  try {
    const result = await bridge().locate();
    if (result.ok) setUser(result);
    else locationLabel.textContent = result.error || "Location unavailable. Set a place.";
  } catch (error) {
    locationLabel.textContent = "Location unavailable. Set a place.";
  }
}

document.getElementById("search-form").addEventListener("submit", (event) => {
  event.preventDefault();
  runSearch(false);
});

document.getElementById("locate-btn").addEventListener("click", async () => {
  locationLabel.textContent = "Checking this PC, then your network…";
  const result = await bridge().detect();
  if (result.ok) setUser(result);
  else locationLabel.textContent = result.error;
});

document.getElementById("place-btn").addEventListener("click", () => {
  if (state.picking) stopPicking(true);
  else startPicking();
});

placeForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const query = placeInput.value.trim();
  if (query.length < 2) return;
  placeMatches.hidden = true;
  placeMatches.innerHTML = "";
  locationLabel.textContent = "Looking up that address…";
  const result = await bridge().find_places(query);
  const matches = result.matches || [];
  if (!result.ok || !matches.length) {
    locationLabel.textContent = result.error || "No matching address. Add the city, for example Ερμού 15, Αθήνα.";
    return;
  }
  if (matches.length === 1) {
    await choosePlace(matches[0]);
    return;
  }
  placeMatches.hidden = false;
  locationLabel.textContent = "Choose the right address.";
  matches.forEach((match) => {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = match.label;
    button.addEventListener("click", () => choosePlace(match));
    item.appendChild(button);
    placeMatches.appendChild(item);
  });
});

async function runSearch(append, filterPath) {
  const query = (append || filterPath ? state.query : queryInput.value).trim();
  if (query.length < 2) {
    showMessage("Keep typing", "Use at least 2 characters.");
    return;
  }
  const token = ++searchToken;
  state.query = query;
  state.product = null;
  if (!append) {
    state.products = [];
    state.overview = [];
    state.nextPage = null;
    if (overviewLayer) overviewLayer.clearLayers();
    if (storeLayer) storeLayer.clearLayers();
    panel.innerHTML = `<div class="message"><span class="spinner"></span>Searching Skroutz…</div>`;
  }
  const result = await bridge().search(query, append ? state.nextPage : filterPath || null);
  if (token !== searchToken) return;
  if (!result.ok) {
    showMessage("Search failed", result.error);
    return;
  }
  const incoming = result.products || [];
  if (append) {
    const seen = new Set(state.products.map((item) => item.id));
    incoming.forEach((item) => {
      if (!seen.has(item.id)) state.products.push(item);
    });
  } else {
    state.products = incoming;
    state.filters = result.filters || { sort: [], groups: [] };
  }
  state.nextPage = result.next_page || null;
  state.total = result.total || state.products.length;
  renderProducts();
  loadStoreSites();
  loadOverview(token);
}

async function loadOverview(token) {
  const items = state.products.map((product) => ({ id: product.id, name: product.name }));
  if (!items.length) {
    state.overview = [];
    drawOverview();
    return;
  }
  if (!state.product && !state.picking) {
    mapNote.hidden = false;
    mapNote.textContent = "Placing these results on the map…";
  }
  const result = await bridge().map_places(items);
  if (token !== searchToken) return;
  state.overview = result.ok ? result.products || [] : [];
  drawOverview(!state.product);
}

let siteToken = 0;

async function loadStoreSites() {
  const token = ++siteToken;
  const items = state.products.map((product) => ({ id: product.id, url: product.url, name: product.name }));
  if (!items.length) return;
  const result = await bridge().store_sites(items);
  if (token !== siteToken || !result.ok) return;
  const byId = new Map((result.sites || []).map((site) => [Number(site.id), site]));
  state.products.forEach((product) => {
    const site = byId.get(Number(product.id));
    if (!site) return;
    product.shop_site = site.shop_site || null;
    product.shop_site_name = site.shop_name || null;
  });
  document.querySelectorAll(".product-block").forEach((block) => {
    const product = state.products.find((item) => String(item.id) === block.dataset.id);
    const button = block.querySelector("[data-open-site]");
    if (!product || !button) return;
    if (!product.shop_site) {
      button.hidden = true;
      return;
    }
    button.hidden = false;
    button.textContent = product.shop_site_name ? `Open at ${product.shop_site_name}` : "Open at shop";
  });
}

function applyFilter(path) {
  if (!path) return;
  runSearch(false, path);
}

function chipButton(option) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "filter-chip" + (option.selected ? " selected" : "");
  const count = option.count == null ? "" : ` ${option.count}`;
  button.textContent = option.name + count;
  button.addEventListener("click", () => applyFilter(option.url));
  return button;
}

function renderFilters() {
  const filters = state.filters || { sort: [], groups: [] };
  if (!filters.sort.length && !filters.groups.length) return;
  const wrap = document.createElement("details");
  wrap.className = "filters";
  const summary = document.createElement("summary");
  const filtersOn = filters.groups.some((group) => group.options.some((option) => option.selected));
  summary.textContent = filtersOn ? "Filters · on" : "Filters";
  if (filtersOn) wrap.open = true;
  wrap.appendChild(summary);

  if (filters.sort.length) {
    const row = document.createElement("label");
    row.className = "sort-row";
    row.append("Sort");
    const select = document.createElement("select");
    filters.sort.forEach((option) => {
      const item = document.createElement("option");
      item.value = option.url;
      item.textContent = option.name;
      item.selected = option.selected;
      select.appendChild(item);
    });
    select.addEventListener("change", () => applyFilter(select.value));
    row.appendChild(select);
    wrap.appendChild(row);
  }

  const selected = filters.groups.flatMap((group) => group.options.filter((option) => option.selected));
  if (selected.length) {
    const clearRow = document.createElement("div");
    clearRow.className = "filter-options";
    selected.forEach((option) => clearRow.appendChild(chipButton(option)));
    const clear = document.createElement("button");
    clear.type = "button";
    clear.className = "filter-chip";
    clear.textContent = "Clear filters";
    clear.addEventListener("click", () => runSearch(false));
    clearRow.appendChild(clear);
    wrap.appendChild(clearRow);
  }

  filters.groups.forEach((group) => {
    const details = document.createElement("details");
    const chosen = group.options.some((option) => option.selected);
    if (chosen) details.open = true;
    const summary = document.createElement("summary");
    summary.textContent = chosen ? `${group.name} · on` : group.name;
    details.appendChild(summary);
    const list = document.createElement("div");
    list.className = "filter-options";
    const first = group.options.slice(0, 8);
    const rest = group.options.slice(8);
    first.forEach((option) => list.appendChild(chipButton(option)));
    if (rest.length) {
      const more = document.createElement("button");
      more.type = "button";
      more.className = "filter-chip";
      more.textContent = `+${rest.length} more`;
      more.addEventListener("click", () => {
        rest.forEach((option) => list.insertBefore(chipButton(option), more));
        more.remove();
      });
      list.appendChild(more);
    }
    details.appendChild(list);
    wrap.appendChild(details);
  });
  panel.appendChild(wrap);
}

function renderProducts() {
  panel.innerHTML = "";
  renderFilters();
  if (!state.products.length) {
    const empty = document.createElement("div");
    empty.className = "message";
    empty.innerHTML = "<strong>No products</strong>Nothing matches these filters. Remove one to see more.";
    panel.appendChild(empty);
    return;
  }
  const count = document.createElement("div");
  count.className = "message";
  count.textContent = `${state.total} results on Skroutz. Showing ${state.products.length}.`;
  panel.appendChild(count);
  state.products.forEach((product) => {
    const block = document.createElement("article");
    block.className = "product-block";
    block.dataset.id = String(product.id);
    const open = state.product && state.product.id === product.id;
    if (open) block.classList.add("open");
    const button = document.createElement("div");
    button.className = "card";
    button.tabIndex = 0;
    const rating = product.rating ? `${product.rating} · ${product.reviews} reviews` : "No reviews yet";
    const shops = product.shop_count ? `${product.shop_count} shops` : "";
    const siteLabel = product.shop_site_name ? `Open at ${product.shop_site_name}` : "Open at shop";
    button.innerHTML = `
      <div class="thumb">${product.image ? `<img alt="" src="${esc(product.image)}" />` : esc(product.name.slice(0, 1))}</div>
      <div class="card-body">
        <h2>${esc(product.name)}</h2>
        <p class="meta">${esc([product.specs, rating, shops].filter(Boolean).join(" · "))}</p>
        <div class="price">${product.price ? `From ${esc(product.price)}` : "See Skroutz"}</div>
        <div class="result-links">
          <button type="button" class="text-link" data-open-product>View product</button>
          <button type="button" class="text-link" data-open-site ${product.shop_site ? "" : "hidden"}>${esc(siteLabel)}</button>
        </div>
        <p class="expand-label">${open ? "Hide stores" : "Show stores"}</p>
      </div>`;
    const stores = document.createElement("div");
    stores.className = "stores";
    stores.hidden = !open;
    button.addEventListener("click", () => toggleProduct(product, block));
    button.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      toggleProduct(product, block);
    });
    const links = button.querySelector(".result-links");
    links.addEventListener("click", (event) => event.stopPropagation());
    links.addEventListener("keydown", (event) => event.stopPropagation());
    links.querySelector("[data-open-product]").addEventListener("click", () => bridge().open_url(product.url));
    links.querySelector("[data-open-site]").addEventListener("click", () => {
      if (product.shop_site) bridge().open_url(product.shop_site);
    });
    block.append(button, stores);
    panel.appendChild(block);
    if (open) renderStores(stores);
  });
  if (state.nextPage) {
    const more = document.createElement("button");
    more.type = "button";
    more.className = "more ghost";
    more.textContent = "More results";
    more.addEventListener("click", () => runSearch(true));
    panel.appendChild(more);
  }
  panel.scrollTop = 0;
}

function closeProduct(block) {
  state.product = null;
  if (block) {
    block.classList.remove("open");
    const stores = block.querySelector(".stores");
    stores.hidden = true;
    stores.innerHTML = "";
    const label = block.querySelector(".expand-label");
    if (label) label.textContent = "Show stores";
  }
  storeLayer.clearLayers();
  drawOverview();
}

function openResult(id) {
  if (state.picking) return;
  const product = state.products.find((item) => Number(item.id) === Number(id));
  const block = document.querySelector(`.product-block[data-id="${id}"]`);
  if (!product || !block) return;
  if (state.product && Number(state.product.id) === Number(id)) return;
  toggleProduct(product, block);
}

async function toggleProduct(product, block) {
  if (state.product && state.product.id === product.id) {
    closeProduct(block);
    return;
  }
  document.querySelectorAll(".product-block.open").forEach((open) => closeProduct(open));
  state.product = product;
  state.radius = "15";
  const stores = block.querySelector(".stores");
  block.classList.add("open");
  stores.hidden = false;
  const label = block.querySelector(".expand-label");
  if (label) label.textContent = "Hide stores";
  drawOverview();
  stores.innerHTML = `<div class="message"><span class="spinner"></span>Loading stores…</div>`;
  block.scrollIntoView({ block: "nearest" });
  const result = await bridge().stores(product.id, product.url);
  if (!state.product || state.product.id !== product.id) return;
  if (!result.ok) {
    stores.innerHTML = `<div class="message"><strong>Stores unavailable</strong>${esc(result.error)}</div>`;
    return;
  }
  state.places = result.places || [];
  annotateDistances();
  renderStores(stores);
}

function annotateDistances() {
  state.places.forEach((place) => {
    place.distance = state.user ? kmBetween(state.user, place) : null;
  });
}

function nearestPlace() {
  return state.places
    .filter((place) => place.distance != null)
    .sort((a, b) => a.distance - b.distance)[0];
}

function sortPlaces(list) {
  const mode = state.storeSort || "distance";
  const missing = (value) => value == null || Number.isNaN(Number(value));
  return list.slice().sort((a, b) => {
    let diff = 0;
    if (mode === "price") {
      if (missing(a.price) && missing(b.price)) diff = 0;
      else if (missing(a.price)) diff = 1;
      else if (missing(b.price)) diff = -1;
      else diff = Number(a.price) - Number(b.price);
    } else if (mode === "rating") {
      if (missing(a.rating_score) && missing(b.rating_score)) diff = 0;
      else if (missing(a.rating_score)) diff = 1;
      else if (missing(b.rating_score)) diff = -1;
      else diff = Number(b.rating_score) - Number(a.rating_score);
    } else if (missing(a.distance) && missing(b.distance)) {
      diff = 0;
    } else if (missing(a.distance)) {
      diff = 1;
    } else if (missing(b.distance)) {
      diff = -1;
    } else {
      diff = a.distance - b.distance;
    }
    if (diff) return diff;
    if (missing(a.distance)) return 1;
    if (missing(b.distance)) return -1;
    return a.distance - b.distance;
  });
}

function visiblePlaces() {
  if (state.radius === "all") return state.places;
  const maxKm = Number(state.radius);
  return state.places.filter((place) => place.distance == null || place.distance <= maxKm);
}

function renderStores(container) {
  const product = state.product;
  const visible = visiblePlaces();
  container.innerHTML = "";
  const tools = document.createElement("div");
  tools.className = "store-tools";
  tools.innerHTML = `
    <select class="radius">
      <option value="5">Within 5 km</option>
      <option value="15">Within 15 km</option>
      <option value="30">Within 30 km</option>
      <option value="60">Within 60 km</option>
      <option value="all">All of Greece</option>
    </select>
    <label class="sort-label">Sort
      <select class="store-sort">
        <option value="distance">Distance</option>
        <option value="price">Price</option>
        <option value="rating">Shop rating</option>
      </select>
    </label>
    <button type="button" class="text-btn compare">Compare prices</button>`;
  container.appendChild(tools);
  tools.querySelector(".radius").value = state.radius;
  tools.querySelector(".radius").addEventListener("change", (event) => {
    state.radius = event.target.value;
    renderStores(container);
  });
  const sortSelect = tools.querySelector(".store-sort");
  sortSelect.value = state.storeSort;
  sortSelect.addEventListener("change", (event) => {
    state.storeSort = event.target.value;
    renderStores(container);
  });
  tools.querySelector(".compare").addEventListener("click", () => bridge().open_skroutz(product.url));

  if (!state.places.length) {
    const empty = document.createElement("div");
    empty.className = "message";
    empty.innerHTML = `<strong>No physical store on the map</strong>Skroutz does not list a shop address for this product. You can still compare online shops on the product page.`;
    container.appendChild(empty);
    drawPlaces([]);
    return;
  }
  let listed = sortPlaces(visible);
  if (!listed.length && state.places.length) {
    const nearest = nearestPlace();
    const empty = document.createElement("div");
    empty.className = "message";
    empty.innerHTML = `<strong>No store within ${esc(state.radius)} km of this address</strong>The closest is ${esc(formatKm(nearest && nearest.distance))} away${nearest && nearest.city ? ` in ${esc(nearest.city)}` : ""}.`;
    container.appendChild(empty);
    listed = sortPlaces(state.places).slice(0, 8);
  }
  if (state.storeSort === "price" && listed.length && listed.every((place) => place.price == null)) {
    const note = document.createElement("p");
    note.className = "sort-note";
    note.textContent = "Skroutz does not list a price for these stores, so they stay in distance order.";
    container.appendChild(note);
  }
  listed.forEach((place, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "store";
    const bits = [
      place.price_label,
      place.rating ? `${place.rating}★` : "",
      formatKm(place.distance),
      place.city,
      place.note,
    ].filter(Boolean);
    button.innerHTML = `
      <div class="index">${index + 1}</div>
      <div>
        <h3>${esc(place.shop_name)}</h3>
        <p class="sub">${esc(place.address)}</p>
        <p class="meta">${esc(bits.join(" · "))}${place.phone ? ` · ${esc(place.phone)}` : ""}</p>
      </div>`;
    button.addEventListener("click", () => focusPlace(place, index + 1));
    container.appendChild(button);
  });
  drawPlaces(listed);
}

function hoursHtml(place) {
  const lines = Object.entries(place.hours || {});
  if (!lines.length) return "";
  return `<ul class="hours">${lines.map(([day, slots]) => `<li>${esc(day)}: ${esc(slots.join(", "))}</li>`).join("")}</ul>`;
}

function popupHtml(place, number) {
  const phone = place.phone ? `<p><a href="tel:${esc(place.phone)}">${esc(place.phone)}</a></p>` : "";
  const shop = place.shop_url
    ? `<p><a href="${esc(place.shop_url)}" data-shop-link>Open shop on Skroutz</a></p>`
    : "";
  const extra = [place.price_label, place.rating ? `${place.rating}★` : "", formatKm(place.distance), place.note]
    .filter(Boolean)
    .join(" · ");
  return `<h3>${number}. ${esc(place.shop_name)}</h3><p>${esc(place.address)}</p><p>${esc(extra)}</p>${phone}${shop}${hoursHtml(place)}`;
}

function drawOverview(fitNearby) {
  if (!overviewLayer) return;
  overviewLayer.clearLayers();
  const selected = state.product ? Number(state.product.id) : null;
  state.overview.forEach((item) => {
    if (selected != null && Number(item.id) === selected) return;
    (item.places || []).forEach((place) => {
      if (place.lat == null || place.lng == null) return;
      const icon = L.divIcon({
        className: "",
        html: '<div class="pin ghost"></div>',
        iconSize: [16, 16],
        iconAnchor: [8, 15],
        tooltipAnchor: [0, -12],
      });
      const marker = L.marker([place.lat, place.lng], { icon, zIndexOffset: -200 }).addTo(overviewLayer);
      const label = [item.name, place.shop_name].filter(Boolean).join(" · ");
      if (label) marker.bindTooltip(esc(label), { direction: "top", opacity: 0.92 });
      marker.on("click", () => openResult(item.id));
    });
  });
  if (state.product || state.picking) return;
  if (state.overview.length) {
    mapNote.hidden = false;
    mapNote.textContent = "Gray pins are every store for these results. Open a result to highlight its stores.";
    if (fitNearby) frameNearbyOverview();
  } else {
    mapNote.hidden = true;
    mapNote.textContent = "";
  }
}

function frameNearbyOverview() {
  if (!state.user || !map) return;
  const near = [];
  state.overview.forEach((item) => {
    (item.places || []).forEach((place) => {
      if (place.lat == null || place.lng == null) return;
      if (kmBetween(state.user, place) <= 15) near.push([place.lat, place.lng]);
    });
  });
  if (!near.length) return;
  map.fitBounds([[state.user.lat, state.user.lng], ...near], { padding: [48, 48], maxZoom: 14 });
}

function drawPlaces(places) {
  storeLayer.clearLayers();
  const bounds = [];
  if (state.user) bounds.push([state.user.lat, state.user.lng]);
  places.forEach((place, index) => {
    const icon = L.divIcon({
      className: "",
      html: `<div class="pin"><span>${index + 1}</span></div>`,
      iconSize: [28, 28],
      iconAnchor: [14, 26],
      popupAnchor: [0, -22],
    });
    const marker = L.marker([place.lat, place.lng], { icon, zIndexOffset: 400 }).addTo(storeLayer);
    marker.bindPopup(popupHtml(place, index + 1));
    marker.on("popupopen", (event) => {
      const link = event.popup.getElement().querySelector("[data-shop-link]");
      if (link) {
        link.addEventListener("click", (click) => {
          click.preventDefault();
          bridge().open_skroutz(place.shop_url);
        });
      }
    });
    place.marker = marker;
    bounds.push([place.lat, place.lng]);
  });
  const limit = state.radius === "all" ? Infinity : Number(state.radius);
  const local = state.user
    ? places.filter((place) => place.distance != null && place.distance <= limit)
    : places;
  if (state.user && local.length && limit !== Infinity) {
    const frame = [[state.user.lat, state.user.lng], ...local.map((place) => [place.lat, place.lng])];
    if (frame.length === 1) map.setView(frame[0], 14);
    else map.fitBounds(frame, { padding: [48, 48], maxZoom: 14 });
  } else if (state.user && !local.length) {
    map.setView([state.user.lat, state.user.lng], 13);
  } else if (bounds.length > 1) {
    map.fitBounds(bounds, { padding: [40, 40], maxZoom: 12 });
  } else if (bounds.length === 1) {
    map.setView(bounds[0], 13);
  }
  const nearest = nearestPlace();
  if (state.user && nearest && limit !== Infinity && nearest.distance > limit) {
    mapNote.hidden = false;
    mapNote.textContent = `No store within ${limit} km. Nearest is ${formatKm(nearest.distance)} from ${state.user.label}.`;
  } else if (!state.user && state.places.length) {
    mapNote.hidden = false;
    mapNote.textContent = "Set your location to sort these stores by distance.";
  } else {
    mapNote.hidden = true;
    mapNote.textContent = "";
  }
  setTimeout(() => map.invalidateSize(), 50);
}

function focusPlace(place, number) {
  if (!place.marker) return;
  map.setView([place.lat, place.lng], Math.max(map.getZoom(), 15));
  place.marker.openPopup();
  document.querySelectorAll(".store").forEach((node, index) => {
    node.classList.toggle("active", index + 1 === number);
  });
}

window.addEventListener("pywebviewready", boot);
