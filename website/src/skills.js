// The skills portal's browse page: search and the category and tag filters. Every card is in the
// page already, built and escaped at build time; this only hides and shows them, and writes
// nothing but plain text. Without it, every skill shows and the tag links still work.
(() => {
  const list = document.getElementById('skill-list');
  if (!list) return;
  const cards = [...list.children];
  const finder = document.getElementById('finder');
  const input = document.getElementById('skill-search');
  const count = document.getElementById('skill-count');
  const none = document.getElementById('skill-none');
  const filters = [...document.querySelectorAll('[data-filter]')];
  const params = new URLSearchParams(location.search);
  let filter = '';
  if (params.get('tag')) filter = `tag:${params.get('tag')}`;
  else if (params.get('category')) filter = `category:${params.get('category')}`;
  input.value = params.get('q') || '';
  finder.hidden = false;

  function matches(card, words) {
    const [kind, value] = filter.split(':');
    if (kind === 'tag' && !card.dataset.tags.split(' ').includes(value)) return false;
    if (kind === 'category' && !card.dataset.categories.split(' ').includes(value)) return false;
    return words.every(word => card.dataset.search.includes(word));
  }

  function apply() {
    const words = input.value.toLowerCase().split(/\s+/).filter(Boolean);
    let shown = 0;
    for (const card of cards) {
      card.hidden = !matches(card, words);
      if (!card.hidden) shown += 1;
    }
    count.textContent = shown === 1 ? '1 skill' : `${shown} skills`;
    none.hidden = shown > 0;
    for (const link of filters) {
      if (link.dataset.filter === filter) link.setAttribute('aria-current', 'true');
      else link.removeAttribute('aria-current');
    }
    const query = new URLSearchParams();
    const [kind, value] = filter.split(':');
    if (kind) query.set(kind, value);
    if (input.value.trim()) query.set('q', input.value.trim());
    const search = query.toString();
    history.replaceState(null, '', search ? `?${search}` : location.pathname);
  }

  for (const link of filters) {
    link.addEventListener('click', event => {
      event.preventDefault();
      filter = link.dataset.filter;
      apply();
    });
  }
  input.addEventListener('input', apply);
  apply();
})();
