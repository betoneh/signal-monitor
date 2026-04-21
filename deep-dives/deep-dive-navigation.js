(function () {
  const DD_READ_KEY = 'sm-dd-read';
  const DD_UNREAD_KEY = 'sm-dd-unread';

  function injectStyles() {
    if (document.getElementById('dd-sequence-nav-style')) return;
    const style = document.createElement('style');
    style.id = 'dd-sequence-nav-style';
    style.textContent = `
      .dd-sequence-nav {
        margin: 2.25rem 0 1.75rem;
        padding-top: 1.25rem;
        border-top: 1px solid rgba(10, 143, 123, 0.28);
      }
      .dd-sequence-label {
        margin-bottom: 0.6rem;
        color: #8b949e;
        font-size: 0.72rem;
        letter-spacing: 0.08em;
        text-transform: uppercase;
      }
      .dd-sequence-primary,
      .dd-sequence-secondary {
        width: 100%;
        border: none;
        background: none;
        padding: 0;
        text-align: left;
        color: inherit;
        cursor: pointer;
        font: inherit;
      }
      .dd-sequence-primary {
        display: grid;
        grid-template-columns: 1fr auto;
        gap: 0.85rem;
        align-items: center;
        color: #eaedf3;
      }
      .dd-sequence-primary:hover .dd-sequence-action,
      .dd-sequence-primary:hover .dd-sequence-target {
        color: #0ec4a9;
      }
      .dd-sequence-copy {
        min-width: 0;
      }
      .dd-sequence-action {
        display: block;
        font-size: 0.98rem;
        line-height: 1.45;
        transition: color 0.15s ease;
      }
      .dd-sequence-target {
        display: block;
        margin-top: 0.28rem;
        color: #8b949e;
        font-size: 0.82rem;
        line-height: 1.55;
        transition: color 0.15s ease;
      }
      .dd-sequence-arrow {
        color: #8b949e;
        font-size: 1.15rem;
        transition: color 0.15s ease, transform 0.15s ease;
      }
      .dd-sequence-primary:hover .dd-sequence-arrow {
        color: #0ec4a9;
        transform: translateX(2px);
      }
      .dd-sequence-secondary {
        margin-top: 0.85rem;
        color: #8b949e;
        font-size: 0.8rem;
        text-decoration: underline;
        text-decoration-style: dashed;
        text-underline-offset: 4px;
        width: fit-content;
      }
      .dd-sequence-secondary:hover {
        color: #eaedf3;
      }
      .dd-sequence-primary[disabled],
      .dd-sequence-secondary[disabled] {
        opacity: 0.55;
        cursor: wait;
      }
      .dd-sequence-status {
        min-height: 1.2rem;
        margin-top: 0.7rem;
        color: #8b949e;
        font-size: 0.78rem;
      }
      .dd-sequence-status.is-error {
        color: #f87171;
      }
      .dd-sequence-status.is-success {
        color: #0ec4a9;
      }
    `;
    document.head.appendChild(style);
  }

  function getLocalList(key) {
    try {
      return JSON.parse(localStorage.getItem(key) || '[]');
    } catch (error) {
      return [];
    }
  }

  function setLocalList(key, list) {
    try {
      localStorage.setItem(key, JSON.stringify(list));
    } catch (error) {}
  }

  function buildLocalState() {
    return {
      read: getLocalList(DD_READ_KEY),
      unreport: getLocalList(DD_UNREAD_KEY),
    };
  }

  function persistState(state) {
    setLocalList(DD_READ_KEY, state.read);
    setLocalList(DD_UNREAD_KEY, state.unreport);
  }

  function mergeState(baseState, remoteState) {
    const merged = {
      read: [...baseState.read],
      unreport: [...baseState.unreport],
    };
    for (const id of remoteState.read || []) {
      if (!merged.read.includes(id)) merged.read.push(id);
    }
    for (const id of remoteState.unreport || []) {
      if (!merged.unreport.includes(id)) merged.unreport.push(id);
    }
    return merged;
  }

  async function fetchRemoteState() {
    try {
      const response = await fetch(`../dd-state.json?_=${Date.now()}`, { cache: 'no-store' });
      if (!response.ok) return { read: [], unreport: [] };
      const state = await response.json();
      return {
        read: Array.isArray(state.read) ? state.read : [],
        unreport: Array.isArray(state.unreport) ? state.unreport : [],
      };
    } catch (error) {
      return { read: [], unreport: [] };
    }
  }

  async function fetchDeepDiveManifest() {
    const response = await fetch(`../index.html?_=${Date.now()}`, { cache: 'no-store' });
    if (!response.ok) {
      throw new Error(`Could not load deep dive manifest (${response.status}).`);
    }

    const html = await response.text();
    const parser = new DOMParser();
    const doc = parser.parseFromString(html, 'text/html');
    const items = Array.from(doc.querySelectorAll('.dd-item[data-dd-id]')).map((el) => {
      const link = el.querySelector('a[href]');
      const title = el.querySelector('.dd-title');
      return {
        id: parseInt(el.dataset.ddId, 10),
        href: link ? link.getAttribute('href') : '',
        title: title ? title.textContent.trim() : `Deep Dive #${el.dataset.ddId}`,
        origReport: el.dataset.origReport === 'true',
      };
    });

    return items
      .filter((item) => item.id && item.href)
      .sort((a, b) => a.id - b.id);
  }

  function isEffectivelyRead(item, state) {
    if (item.origReport) {
      return !state.unreport.includes(item.id);
    }
    return state.read.includes(item.id);
  }

  function markAsRead(item, state) {
    if (item.origReport) {
      state.unreport = state.unreport.filter((id) => id !== item.id);
      return;
    }
    if (!state.read.includes(item.id)) {
      state.read = [...state.read, item.id];
    }
  }

  function findCurrentItem(items) {
    const currentName = window.location.pathname.split('/').pop();
    return items.find((item) => item.href.split('/').pop() === currentName) || null;
  }

  function findNextUnread(items, currentItem, state) {
    const currentIndex = items.findIndex((item) => item.id === currentItem.id);
    if (currentIndex < 0) return null;
    return items.slice(currentIndex + 1).find((item) => !isEffectivelyRead(item, state)) || null;
  }

  function getPageHref(item) {
    return item.href.split('/').pop();
  }

  function setStatus(root, message, tone) {
    const status = root.querySelector('.dd-sequence-status');
    if (!status) return;
    status.textContent = message || '';
    status.classList.toggle('is-error', tone === 'error');
    status.classList.toggle('is-success', tone === 'success');
  }

  function setBusy(root, busy) {
    root.querySelectorAll('button').forEach((button) => {
      button.disabled = busy;
    });
  }

  function buildPrimaryCopy(nextItem) {
    if (!nextItem) {
      return {
        action: 'Mark as read and return to Deep Dives',
        target: 'No later unread deep dives from here.',
      };
    }
    return {
      action: 'Mark as read and go to next deep dive',
      target: `Next unread: #${nextItem.id} — ${nextItem.title}`,
    };
  }

  function createNavigation(rootPoint, currentItem, primaryTarget, secondaryTarget, onPrimary, onSecondary) {
    const nav = document.createElement('section');
    nav.className = 'dd-sequence-nav';

    const primaryCopy = buildPrimaryCopy(primaryTarget);
    nav.innerHTML = `
      <div class="dd-sequence-label">Continue Reading</div>
      <button type="button" class="dd-sequence-primary">
        <span class="dd-sequence-copy">
          <span class="dd-sequence-action">${primaryCopy.action}</span>
          <span class="dd-sequence-target">${primaryCopy.target}</span>
        </span>
        <span class="dd-sequence-arrow" aria-hidden="true">→</span>
      </button>
      ${
        secondaryTarget
          ? `<button type="button" class="dd-sequence-secondary">Next unread without marking this one</button>`
          : ''
      }
      <div class="dd-sequence-status" aria-live="polite"></div>
    `;

    nav.querySelector('.dd-sequence-primary').addEventListener('click', onPrimary);
    const secondaryButton = nav.querySelector('.dd-sequence-secondary');
    if (secondaryButton) {
      secondaryButton.addEventListener('click', onSecondary);
    }

    rootPoint.insertAdjacentElement('afterend', nav);
    return nav;
  }

  async function init() {
    injectStyles();

    const rootPoint = document.querySelector('.source') || document.querySelector('.content');
    if (!rootPoint) return;

    const [items, remoteState] = await Promise.all([fetchDeepDiveManifest(), fetchRemoteState()]);
    const currentItem = findCurrentItem(items);
    if (!currentItem) return;

    let state = mergeState(buildLocalState(), remoteState);
    persistState(state);

    const primaryState = {
      read: [...state.read],
      unreport: [...state.unreport],
    };
    markAsRead(currentItem, primaryState);

    const primaryTarget = findNextUnread(items, currentItem, primaryState);
    const secondaryTarget = findNextUnread(items, currentItem, state);

    const nav = createNavigation(
      rootPoint,
      currentItem,
      primaryTarget,
      secondaryTarget,
      async () => {
        try {
          setBusy(nav, true);
          setStatus(nav, 'Marked locally. Moving on…');

          state = {
            read: [...state.read],
            unreport: [...state.unreport],
          };
          markAsRead(currentItem, state);
          persistState(state);
          setStatus(nav, 'Marked locally. Save later from the index.', 'success');

          const destination = primaryTarget ? getPageHref(primaryTarget) : '../index.html';
          try {
            localStorage.setItem('sm-tab', 'deepdives');
          } catch (error) {}
          window.location.href = destination;
        } catch (error) {
          setBusy(nav, false);
          setStatus(nav, error.message || 'Could not save read state.', 'error');
        }
      },
      () => {
        if (!secondaryTarget) return;
        try {
          localStorage.setItem('sm-tab', 'deepdives');
        } catch (error) {}
        window.location.href = getPageHref(secondaryTarget);
      }
    );
  }

  init().catch((error) => {
    console.error('Deep dive navigation failed:', error);
  });
})();
