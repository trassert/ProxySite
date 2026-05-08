/**
 * MTProto Proxy Hub - Frontend JavaScript
 * Handles voting, copy, and dynamic updates
 */

// ============================================
// Theme Toggle
// ============================================

function toggleTheme() {
  const html = document.documentElement;
  const isDark = html.classList.contains('dark');
  
  if (isDark) {
    html.classList.remove('dark');
    localStorage.setItem('theme', 'light');
  } else {
    html.classList.add('dark');
    localStorage.setItem('theme', 'dark');
  }
}

// ============================================
// Snackbar
// ============================================

function showSnackbar(message, duration = 3000) {
  // Remove existing snackbar
  const existing = document.querySelector('.snackbar');
  if (existing) {
    existing.remove();
  }

  const snackbar = document.createElement('div');
  snackbar.className = 'snackbar';
  snackbar.textContent = message;
  document.body.appendChild(snackbar);

  // Trigger animation
  requestAnimationFrame(() => {
    snackbar.classList.add('show');
  });

  setTimeout(() => {
    snackbar.classList.remove('show');
    setTimeout(() => snackbar.remove(), 300);
  }, duration);
}

// ============================================
// Voting
// ============================================

async function vote(proxyId, voteType) {
  const btn = document.querySelector(`[data-proxy-id="${proxyId}"][data-vote="${voteType}"]`);
  if (!btn) return;

  btn.classList.add('loading');

  try {
    const response = await fetch('/api/vote', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        proxy_id: proxyId,
        vote_type: voteType,
      }),
    });

    const data = await response.json();

    if (response.ok) {
      // Update counts
      const card = btn.closest('.proxy-card');
      const likeBtn = card.querySelector('[data-vote="like"]');
      const dislikeBtn = card.querySelector('[data-vote="dislike"]');

      likeBtn.querySelector('.count').textContent = data.likes;
      dislikeBtn.querySelector('.count').textContent = data.dislikes;

      // Update active state
      if (data.success) {
        if (voteType === 'like') {
          likeBtn.classList.add('active');
          dislikeBtn.classList.remove('active');
        } else {
          dislikeBtn.classList.add('active');
          likeBtn.classList.remove('active');
        }
        showSnackbar(voteType === 'like' ? 'Liked!' : 'Disliked!');
      } else {
        showSnackbar(data.message || 'Already voted');
      }
    } else {
      showSnackbar('Error: ' + (data.detail || 'Unknown error'));
    }
  } catch (error) {
    console.error('Vote error:', error);
    showSnackbar('Network error');
  } finally {
    btn.classList.remove('loading');
  }
}

// ============================================
// Copy to clipboard
// ============================================

async function copyLink(link, element) {
  try {
    await navigator.clipboard.writeText(link);
    element.classList.add('copy-success');
    setTimeout(() => element.classList.remove('copy-success'), 300);
    showSnackbar('Link copied!');
  } catch (error) {
    // Fallback
    const textarea = document.createElement('textarea');
    textarea.value = link;
    textarea.style.position = 'fixed';
    textarea.style.opacity = '0';
    document.body.appendChild(textarea);
    textarea.select();
    document.execCommand('copy');
    document.body.removeChild(textarea);
    showSnackbar('Link copied!');
  }
}

// ============================================
// Sorting
// ============================================

function sortBy(sort) {
  const url = new URL(window.location);
  url.searchParams.set('sort', sort);
  window.location.href = url.toString();
}

// ============================================
// Dialog
// ============================================

function openDialog(dialogId) {
  const backdrop = document.getElementById(dialogId);
  if (backdrop) {
    backdrop.classList.add('open');
    document.body.style.overflow = 'hidden';
  }
}

function closeDialog(dialogId) {
  const backdrop = document.getElementById(dialogId);
  if (backdrop) {
    backdrop.classList.remove('open');
    document.body.style.overflow = '';
  }
}

// Close on backdrop click
document.addEventListener('click', (e) => {
  if (e.target.classList.contains('dialog-backdrop')) {
    e.target.classList.remove('open');
    document.body.style.overflow = '';
  }
});

// Close on escape
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    const openBackdrop = document.querySelector('.dialog-backdrop.open');
    if (openBackdrop) {
      openBackdrop.classList.remove('open');
      document.body.style.overflow = '';
    }
  }
});

// ============================================
// Form handling
// ============================================

function switchTab(tabId) {
  // Update active tab button
  const tabs = document.querySelectorAll('.form-tab');
  tabs.forEach(tab => {
    const isActive = tab.dataset.tab === tabId;
    tab.classList.toggle('active', isActive);
  });

  // Show/hide panels - explicitly set display
  const panels = document.querySelectorAll('.form-panel');
  panels.forEach(panel => {
    if (panel.id === tabId) {
      panel.classList.remove('hidden');
      panel.style.display = 'block';
    } else {
      panel.classList.add('hidden');
      panel.style.display = 'none';
    }
  });
}

// ============================================
// Load user votes on page load
// ============================================

async function loadUserVotes() {
  const cards = document.querySelectorAll('.proxy-card');

  for (const card of cards) {
    const proxyId = card.dataset.proxyId;
    if (!proxyId) continue;

    try {
      const response = await fetch(`/api/vote/${proxyId}`);
      const data = await response.json();

      if (data.vote) {
        const btn = card.querySelector(`[data-vote="${data.vote}"]`);
        if (btn) {
          btn.classList.add('active');
        }
      }
    } catch (error) {
      // Ignore errors for individual vote loading
    }
  }
}

// Load votes when page loads
document.addEventListener('DOMContentLoaded', loadUserVotes);

// ============================================
// Trigger ping check
// ============================================

// ============================================
// Form submission for adding proxies
// ============================================

async function submitAddProxy() {
  const tabId = getActiveTab();
  const data = {};

  if (tabId === 'manual-tab') {
    const server = document.getElementById('proxy-server')?.value?.trim();
    const port = document.getElementById('proxy-port')?.value?.trim();
    const secret = document.getElementById('proxy-secret')?.value?.trim();

    if (!server || !port || !secret) {
      showSnackbar('Please fill all fields');
      return;
    }

    data.server = server;
    data.port = parseInt(port);
    data.secret = secret;
  } else if (tabId === 'links-tab') {
    const links = document.getElementById('proxy-links')?.value?.trim();
    if (!links) {
      showSnackbar('Please paste at least one proxy link');
      return;
    }
    data.links = links;
  } else {
    showSnackbar('Please select a tab');
    return;
  }

  const btn = document.querySelector('button[onclick*="submitAddProxy"]');
  if (btn) btn.classList.add('loading');

  try {
    const response = await fetch('/api/add-proxy', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(data),
    });

    const result = await response.json();

    if (result.added > 0) {
      showSnackbar(`Added ${result.added} proxy/proxies!`);
      // Clear form - use value property not attribute
      const serverInput = document.getElementById('proxy-server');
      const portInput = document.getElementById('proxy-port');
      const secretInput = document.getElementById('proxy-secret');
      const linksInput = document.getElementById('proxy-links');
      if (serverInput) serverInput.value = '';
      if (portInput) portInput.value = '';
      if (secretInput) secretInput.value = '';
      if (linksInput) linksInput.value = '';
      // Reload list
      setTimeout(() => location.reload(), 500);
    } else if (result.duplicates > 0) {
      showSnackbar(`${result.duplicates} proxy/proxies already exist`);
    }

    if (result.errors?.length > 0) {
      showSnackbar(`Errors: ${result.errors.join(', ')}`);
    }
  } catch (error) {
    console.error('Submit error:', error);
    showSnackbar('Network error');
  } finally {
    if (btn) btn.classList.remove('loading');
  }
}

function getActiveTab() {
  const activeTab = document.querySelector('.form-tab.active');
  return activeTab?.dataset?.tab || 'manual-tab';
}

async function checkPing(proxyId) {
  const card = document.querySelector(`[data-proxy-id="${proxyId}"]`);
  if (!card) return;

  const badge = card.querySelector('.ping-badge');
  if (!badge) return;

  badge.className = 'ping-badge pending';
  badge.innerHTML = `
    <svg class="icon" viewBox="0 0 24 24">
      <path d="M12,4V2A10,10 0 0,0 2,12H4A8,8 0 0,1 12,4Z">
        <animateTransform attributeName="transform" type="rotate" from="0 12 12" to="360 12 12" dur="1s" repeatCount="indefinite"/>
      </path>
    </svg>
    Checking...
  `;

  try {
    const response = await fetch(`/api/ping/${proxyId}`, { method: 'POST' });
    const data = await response.json();

    badge.className = `ping-badge ${data.status}`;

    let statusIcon = '';
    let statusText = '';

    switch (data.status) {
      case 'ok':
        statusIcon = '<path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41L9 16.17z"/>';
        statusText = `${data.ping_ms}ms`;
        break;
      case 'warning':
        statusIcon = '<path d="M11.99 2C6.47 2 2 6.48 2 12s4.47 10 9.99 10C17.52 22 22 17.52 22 12S17.52 2 11.99 2zM12 20c-4.42 0-8-3.58-8-8s3.58-8 8-8 8 3.58 8 8-3.58 8-8 8zm.5-13H11v6l5.25 3.15.75-1.23-4.5-2.67V7z"/>';
        statusText = `${data.ping_ms}ms`;
        break;
      case 'failed':
        statusIcon = '<path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z"/>';
        statusText = 'Down';
        break;
    }

    badge.innerHTML = `
      <svg class="icon" viewBox="0 0 24 24">${statusIcon}</svg>
      ${statusText}
    `;

    showSnackbar(`Ping: ${data.status}`);
  } catch (error) {
    badge.className = 'ping-badge failed';
    badge.innerHTML = `
      <svg class="icon" viewBox="0 0 24 24">
        <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z"/>
      </svg>
      Error
    `;
    showSnackbar('Ping check failed');
  }
}
