// Brewhouse storefront script
async function loadStats() {
  const res = await fetch('/api/stat');
  const data = await res.json();
  document.getElementById('stat-total').textContent = data.total;
  document.getElementById('stat-latest').textContent = data.latest;
}

document.querySelectorAll('.buy').forEach((btn) => {
  btn.addEventListener('click', () => {
    btn.textContent = 'Added';
  });
});

document.getElementById('signup-form').addEventListener('submit', (e) => {
  e.preventDefault();
  alert('Thanks for subscribing');
});

loadStats();

// Promo banner (the element was removed from the page in a redesign)
document.querySelector('#promo-banner').textContent = 'Free shipping over $30';
