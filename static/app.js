const tg = window.Telegram?.WebApp;
tg?.ready();
tg?.expand();

const state = {
rarity: "All",
characters: [],
currency: "silver"
};

const list = document.getElementById("characters");
const statusText = document.getElementById("status");
const budgetInput = document.getElementById("budget");
const currencySelect = document.getElementById("currency");

function escapeHTML(value) {
return String(value ?? "").replace(/[&<>"']/g, char => ({
"&": "&",
"<": "<",
">": ">",
'"': """,
"'": "'"
})[char]);
}

function money(character) {
const prices = character.prices || {};
return Number(prices[state.currency] ?? character.price ?? 0);
}

function renderCharacters() {
const budgetText = budgetInput.value.trim();
const budget = budgetText === "" ? Infinity : Number(budgetText);

const filtered = state.characters.filter(character => {
const matchesRarity =
state.rarity === "All" || character.rarity === state.rarity;

const matchesBudget =
  Number.isFinite(budget) && budget >= 0
    ? money(character) <= budget
    : budget === Infinity;

return matchesRarity && matchesBudget;

});

if (!filtered.length) {
list.innerHTML =
'<p class="empty">No characters match these filters.</p >';
return;
}

list.innerHTML = filtered.map(character => {
const name = escapeHTML(character.name);
const rarity = escapeHTML(character.rarity);
const image = escapeHTML(character.image_url || character.image || "");
const price = money(character);

return `
  <article class="character">
    <img
      src="${image}"
      alt="${name}"
      loading="lazy"
      onerror="this.style.display='none'"
    >
    <div class="character-info">
      <h3>${name}</h3>
      <p>✨ ${rarity}</p >
      <p>Price: ${price.toLocaleString()} ${escapeHTML(state.currency)}</p >
      <button
        type="button"
        data-buy="${encodeURIComponent(character.name)}"
      >Buy character</button>
    </div>
  </article>
`;

}).join("");
}

async function loadMarket() {
statusText.textContent = "Loading market…";

try {
const response = await fetch("/api/market", {
headers: { "Accept": "application/json" }
});

if (!response.ok) {
  throw new Error(`Market request failed (${response.status})`);
}

const data = await response.json();

state.characters = Array.isArray(data)
  ? data
  : (data.characters || []);

if (data.balance) {
  document.getElementById("silver").textContent =
    data.balance.silver ?? "—";
  document.getElementById("gold").textContent =
    data.balance.gold ?? "—";
  document.getElementById("diamond").textContent =
    data.balance.diamond ?? "—";
}

statusText.textContent =
  `${state.characters.length} characters available`;

renderCharacters();

} catch (error) {
console.error(error);
statusText.textContent =
"The market backend is not connected yet. Please try again later.";
list.innerHTML =
'<p class="empty">Characters will appear when the backend is ready.</p >';
}
}

document.getElementById("rarities").addEventListener("click", event => {
const button = event.target.closest("button[data-rarity]");
if (!button) return;

state.rarity = button.dataset.rarity;

document.querySelectorAll("#rarities button").forEach(item => {
item.classList.toggle("active", item === button);
});

renderCharacters();
});

currencySelect.addEventListener("change", () => {
state.currency = currencySelect.value;
renderCharacters();
});

document.getElementById("filterButton").addEventListener("click", () => {
renderCharacters();
});

document.getElementById("resetButton").addEventListener("click", () => {
budgetInput.value = "";
state.rarity = "All";
state.currency = "silver";
currencySelect.value = "silver";

document.querySelectorAll("#rarities button").forEach(button => {
button.classList.toggle("active", button.dataset.rarity === "All");
});

renderCharacters();
});

list.addEventListener("click", async event => {
const button = event.target.closest("button[data-buy]");
if (!button) return;

const name = decodeURIComponent(button.dataset.buy);

if (!tg?.initData) {
alert("Open this market from the Telegram bot to purchase characters.");
return;
}

const confirmed = confirm("Request to buy ${name}?");
if (!confirmed) return;

button.disabled = true;

try {
const response = await fetch("/api/buy", {
method: "POST",
headers: {
"Content-Type": "application/json",
"Accept": "application/json"
},
body: JSON.stringify({
initData: tg.initData,
character_name: name,
currency: state.currency
})
});

const result = await response.json();

if (!response.ok) {
  throw new Error(result.error || "Purchase failed.");
}

alert(result.message || "Purchase successful!");
await loadMarket();

} catch (error) {
alert(error.message || "Could not complete purchase.");
} finally {
button.disabled = false;
}
});

loadMarket();
