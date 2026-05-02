(function () {
  if (window.__smDeepDiveNavigationLoaded) return;
  window.__smDeepDiveNavigationLoaded = true;

  const DD_READ_KEY = 'sm-dd-read';
  const DD_UNREAD_KEY = 'sm-dd-unread';

  function injectStyles() {
    if (document.getElementById('dd-sequence-nav-style')) return;
    const style = document.createElement('style');
    style.id = 'dd-sequence-nav-style';
    style.textContent = `
      .dd-sequence-divider {
        margin: 1.5rem 0;
        border: 0;
        border-top: 1px dashed rgba(139, 148, 158, 0.45);
      }
      .dd-sequence-nav {
        display: flex;
        flex-direction: column;
        gap: 1.5rem;
      }
      .dd-sequence-index,
      .dd-sequence-primary {
        border: none;
        background: none;
        padding: 0;
        text-align: left;
        color: inherit;
        cursor: pointer;
        font: inherit;
      }
      .dd-sequence-index,
      .dd-sequence-primary {
        display: flex;
        gap: 0.5rem;
        align-items: flex-start;
        color: #eaedf3;
        width: auto;
        text-decoration: none;
      }
      .dd-sequence-index {
        justify-content: flex-start;
        text-align: left;
      }
      .dd-sequence-primary {
        justify-content: flex-end;
        text-align: right;
        margin-left: auto;
      }
      .dd-sequence-copy {
        min-width: 0;
      }
      .dd-sequence-action {
        display: block;
        font-size: 0.875rem;
        line-height: 1.25rem;
        color: #8b949e;
      }
      .dd-sequence-target {
        display: block;
        color: rgba(14, 196, 169, 0.85);
        margin-top: 0;
      }
      .dd-sequence-arrow {
        color: currentColor;
        flex-shrink: 0;
        margin-top: 0.125rem;
      }
      .dd-sequence-index:hover,
      .dd-sequence-primary:hover {
        opacity: 0.75;
      }
      .dd-sequence-index svg,
      .dd-sequence-primary svg {
        width: 24px;
        height: 24px;
        fill: none;
        stroke: currentColor;
        stroke-width: 2;
        stroke-linecap: round;
        stroke-linejoin: round;
      }
      .dd-sequence-primary[disabled],
      .dd-sequence-index[disabled] {
        opacity: 0.55;
        cursor: wait;
      }
      .dd-sequence-status {
        min-height: 1.2rem;
        text-align: right;
        color: #8b949e;
        font-size: 0.78rem;
      }
      .dd-sequence-status.is-error {
        color: #f87171;
      }
      .dd-sequence-status.is-success {
        color: #0ec4a9;
      }
      @media (min-width: 640px) {
        .dd-sequence-nav {
          flex-direction: row;
          justify-content: space-between;
          gap: 1.5rem;
          align-items: flex-start;
        }
        .dd-sequence-meta {
          margin-left: auto;
          text-align: right;
        }
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
    root.querySelectorAll('a.dd-sequence-primary').forEach((link) => {
      link.style.pointerEvents = busy ? 'none' : '';
    });
  }

  function buildPrimaryCopy(nextItem) {
    if (!nextItem) {
      return {
        action: 'Deep Dives',
        target: 'Return to index',
      };
    }
    return {
      action: 'Next Deep Dive',
      target: `#${nextItem.id} — ${nextItem.title}`,
    };
  }

  function createNavigation(rootPoint, currentItem, primaryTarget, onPrimary, onIndex) {
    const nav = document.createElement('section');
    nav.className = 'dd-sequence-nav';

    const primaryCopy = buildPrimaryCopy(primaryTarget);
    nav.innerHTML = `
      <a href="../index.html" class="dd-sequence-index" id="deep-dive-index-link">
        <svg viewBox="0 0 24 24" aria-hidden="true" class="dd-sequence-arrow">
          <path stroke="none" d="M0 0h24v24H0z" fill="none"></path>
          <path d="M15 6l-6 6l6 6"></path>
        </svg>
        <div class="dd-sequence-copy">
          <span class="dd-sequence-action">Deep Dive Index</span>
          <div class="dd-sequence-target">Mark as read and return to index</div>
        </div>
      </a>
      <div class="dd-sequence-meta">
        <a href="${primaryTarget ? getPageHref(primaryTarget) : '../index.html'}" class="dd-sequence-primary" id="next-post-link">
          <div class="dd-sequence-copy">
            <span class="dd-sequence-action">${primaryCopy.action}</span>
            <div class="dd-sequence-target">${primaryCopy.target}</div>
          </div>
          <svg viewBox="0 0 24 24" aria-hidden="true" class="dd-sequence-arrow">
            <path stroke="none" d="M0 0h24v24H0z" fill="none"></path>
            <path d="M9 6l6 6l-6 6"></path>
          </svg>
        </a>
        <div class="dd-sequence-status" aria-live="polite"></div>
      </div>
    `;

    nav.querySelector('.dd-sequence-primary').addEventListener('click', onPrimary);
    nav.querySelector('.dd-sequence-index').addEventListener('click', onIndex);

    const divider = document.createElement('hr');
    divider.className = 'dd-sequence-divider';
    rootPoint.insertAdjacentElement('afterend', divider);
    divider.insertAdjacentElement('afterend', nav);
    return nav;
  }

  async function init() {
    injectStyles();

    const rootPoint = document.querySelector('.content');
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
    const nav = createNavigation(
      rootPoint,
      currentItem,
      primaryTarget,
      async (event) => {
        event.preventDefault();
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
      async (event) => {
        event.preventDefault();
        try {
          setBusy(nav, true);
          setStatus(nav, 'Marked locally. Returning to index…');

          state = {
            read: [...state.read],
            unreport: [...state.unreport],
          };
          markAsRead(currentItem, state);
          persistState(state);

          try {
            localStorage.setItem('sm-tab', 'deepdives');
          } catch (error) {}
          window.location.href = '../index.html';
        } catch (error) {
          setBusy(nav, false);
          setStatus(nav, error.message || 'Could not save read state.', 'error');
        }
      }
    );
  }

  init().catch((error) => {
    console.error('Deep dive navigation failed:', error);
  });
})();
