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
    const response = await fetch('./api/vote', {
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
        
        // Re-sort by likes immediately when liked - move card to new position
        const currentSort = new URLSearchParams(window.location.search).get('sort') || 'likes';
        if (currentSort === 'likes' && voteType === 'like' && data.position !== null && data.position >= 0) {
          await moveProxyCardToPosition(proxyId, data.position);
        }
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
      const response = await fetch(`./api/vote/${proxyId}`);
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
    const response = await fetch('./api/add-proxy', {
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
    const response = await fetch(`./api/ping/${proxyId}`, { method: 'POST' });
    const data = await response.json();

    // Determine badge class based on status and fallback
    let badgeClass = data.status;
    if (data.is_fallback) {
      badgeClass = 'fallback';
    }
    badge.className = `ping-badge ${badgeClass}`;

    let statusIcon = '';
    let statusText = '';

    switch (data.status) {
      case 'ok':
        if (data.is_fallback) {
          // Fallback proxy - show warning icon with exclamation
          statusIcon = '<path d="M1 21h22L12 2 1 21zm12-3h-2v-2h2v2zm0-4h-2v-4h2v4z"/>';
          statusText = 'TCP OK';
        } else {
          statusIcon = '<path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41L9 16.17z"/>';
          statusText = `${data.ping_ms || ''}ms`;
        }
        break;
      case 'warning':
        if (data.is_fallback) {
          // TCP fallback succeeded but proxy-get failed
          statusIcon = '<path d="M1 21h22L12 2 1 21zm12-3h-2v-2h2v2zm0-4h-2v-4h2v4z"/>';
          statusText = 'TCP Only';
        } else {
          statusIcon = '<path d="M11.99 2C6.47 2 2 6.48 2 12s4.47 10 9.99 10C17.52 22 22 17.52 22 12S17.52 2 11.99 2zM12 20c-4.42 0-8-3.58-8-8s3.58-8 8-8 8 3.58 8 8-3.58 8-8 8zm.5-13H11v6l5.25 3.15.75-1.23-4.5-2.67V7z"/>';
          statusText = `${data.ping_ms || ''}ms`;
        }
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

    // Update stats bar
    await updateStats();

    // Re-sort list if sorted by ping
    const currentSort = new URLSearchParams(window.location.search).get('sort') || 'likes';
    if (currentSort === 'ping') {
      await refreshProxyList('ping');
    } else {
      showSnackbar(data.is_fallback ? 'TCP fallback OK' : `Ping: ${data.status}`);
    }
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

// ============================================
// QR Code
// ============================================

async function updateStats() {
  try {
    const response = await fetch('./api/stats');
    const data = await response.json();
    
    // Update stat chips
    const statChips = document.querySelectorAll('.stat-chip');
    statChips.forEach(chip => {
      const valueSpan = chip.querySelector('.value');
      if (!valueSpan) return;
      
      if (chip.textContent.includes('proxies')) {
        valueSpan.textContent = data.total_proxies;
      } else if (chip.textContent.includes('likes')) {
        valueSpan.textContent = data.total_likes;
      } else if (chip.textContent.includes('online')) {
        valueSpan.textContent = data.online_count || 0;
      } else if (chip.textContent.includes('avg')) {
        if (data.avg_ping_ms) {
          valueSpan.textContent = `${data.avg_ping_ms}ms`;
        }
      }
    });
  } catch (error) {
    console.error('Failed to update stats:', error);
  }
}

async function refreshProxyList(sortBy = 'likes') {
  try {
    const response = await fetch(`./api/proxies?sort=${sortBy}&limit=100`);
    const data = await response.json();
    
    const proxyList = document.querySelector('.proxy-list');
    if (!proxyList) return;
    
    // Clear current list
    proxyList.innerHTML = '';
    
    // Render new proxies
    data.proxies.forEach(proxy => {
      const card = createProxyCard(proxy);
      proxyList.appendChild(card);
    });
    
    // Re-load user votes
    await loadUserVotes();
  } catch (error) {
    console.error('Failed to refresh proxy list:', error);
  }
}

function createProxyCard(proxy) {
  const card = document.createElement('article');
  card.className = 'proxy-card';
  if (proxy.is_fallback) {
    card.classList.add('proxy-card-fallback');
  }
  card.dataset.proxyId = proxy.id;
  
  // Determine badge class based on status and fallback
  let badgeClass = proxy.ping_status;
  if (proxy.is_fallback) {
    badgeClass = 'fallback';
  }
  
  let pingBadgeContent = '';
  switch (proxy.ping_status) {
    case 'ok':
      if (proxy.is_fallback) {
        // TCP fallback succeeded - show TCP ping time
        const tcpPing = proxy.ping_ms ?? 0;
        pingBadgeContent = `
          <svg class="icon" viewBox="0 0 24 24"><path d="M1 21h22L12 2 1 21zm12-3h-2v-2h2v2zm0-4h-2v-4h2v4z"/></svg>
          ${tcpPing}ms
        `;
      } else {
        const pingVal = proxy.ping_ms ?? 0;
        pingBadgeContent = `
          <svg class="icon" viewBox="0 0 24 24"><path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41L9 16.17z"/></svg>
          ${pingVal}ms
        `;
      }
      break;
    case 'warning':
      if (proxy.is_fallback) {
        // TCP fallback succeeded but proxy-get failed
        const tcpPing = proxy.ping_ms ?? 0;
        pingBadgeContent = `
          <svg class="icon" viewBox="0 0 24 24"><path d="M1 21h22L12 2 1 21zm12-3h-2v-2h2v2zm0-4h-2v-4h2v4z"/></svg>
          ${tcpPing}ms
        `;
      } else {
        const pingVal = proxy.ping_ms ?? 0;
        pingBadgeContent = `
          <svg class="icon" viewBox="0 0 24 24"><path d="M11.99 2C6.47 2 2 6.48 2 12s4.47 10 9.99 10C17.52 22 22 17.52 22 12S17.52 2 11.99 2zM12 20c-4.42 0-8-3.58-8-8s3.58-8 8-8 8 3.58 8 8-3.58 8-8 8zm.5-13H11v6l5.25 3.15.75-1.23-4.5-2.67V7z"/></svg>
          ${pingVal}ms
        `;
      }
      break;
    case 'failed':
      pingBadgeContent = `
        <svg class="icon" viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z"/></svg>
        Down
      `;
      break;
    default:
      pingBadgeContent = `
        <svg class="icon" viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 18c-4.41 0-8-3.59-8-8s3.59-8 8-8 8 3.59 8 8-3.59 8-8 8z"/></svg>
        Pending
      `;
  }
  
  card.innerHTML = `
    <div class="proxy-header">
      <div>
        <h2 class="proxy-server">${proxy.server}<span class="proxy-port">:${proxy.port}</span></h2>
      </div>
      <button class="ping-badge ${badgeClass}" onclick="checkPing(${proxy.id})" title="Click to refresh">
        ${pingBadgeContent}
      </button>
    </div>

    <div class="proxy-secret">${proxy.secret}</div>

    <div class="proxy-actions">
      <div class="vote-buttons">
        <button 
          class="vote-btn like" 
          data-proxy-id="${proxy.id}" 
          data-vote="like"
          onclick="vote(${proxy.id}, 'like')"
        >
          <svg class="icon" viewBox="0 0 24 24"><path d="M1 21h4V9H1v12zm22-11c0-1.1-.9-2-2-2h-6.31l.95-4.57.03-.32c0-.41-.17-.79-.44-1.06L14.17 1 7.59 7.59C7.22 7.95 7 8.45 7 9v10c0 1.1.9 2 2 2h9c.83 0 1.54-.5 1.84-1.22l3.02-7.05c.09-.23.14-.47.14-.73v-2z"/></svg>
          <span class="count">${proxy.likes}</span>
        </button>
        <button 
          class="vote-btn dislike" 
          data-proxy-id="${proxy.id}" 
          data-vote="dislike"
          onclick="vote(${proxy.id}, 'dislike')"
        >
          <svg class="icon" viewBox="0 0 24 24"><path d="M15 3H6c-.83 0-1.54.5-1.84 1.22l-3.02 7.05c-.09.23-.14.47-.14.73v2c0 1.1.9 2 2 2h6.31l-.95 4.57-.03.32c0 .41.17.79.44 1.06L9.83 23l6.59-6.59c.36-.36.58-.86.58-1.41V5c0-1.1-.9-2-2-2zm4 0v12h4V3h-4z"/></svg>
          <span class="count">${proxy.dislikes}</span>
        </button>
      </div>

      <div class="link-buttons">
        <a 
          href="tg://proxy?server=${proxy.server}&port=${proxy.port}&secret=${proxy.secret}" 
          class="link-btn"
        >
          <svg class="icon" viewBox="0 0 24 24"><path d="M9.78 18.65l.28-4.23 7.68-6.92c.34-.31-.07-.46-.52-.19L7.74 13.3 3.64 12c-.88-.25-.89-.86.2-1.3l15.97-6.16c.73-.33 1.43.18 1.15 1.3l-2.72 12.81c-.19.91-.74 1.13-1.5.71L12.6 16.3l-1.99 1.93c-.23.23-.42.42-.83.42z"/></svg>
          Open
        </a>
        <button 
          class="link-btn" 
          onclick="copyLink('tg://proxy?server=${proxy.server}&port=${proxy.port}&secret=${proxy.secret}', this)"
        >
          <svg class="icon" viewBox="0 0 24 24"><path d="M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z"/></svg>
          Copy
        </button>
        <button 
          class="link-btn" 
          onclick="showQRCode('${proxy.server}', '${proxy.port}', '${proxy.secret}')"
        >
          <svg class="icon" viewBox="0 0 24 24"><path d="M3 3h8v8H3V3m2 2v4h4V5H5m8-2h8v8h-8V3m2 2v4h4V5h-4M3 13h8v8H3v-8m2 2v4h4v-4H5m10-4h2v2h-2v-2m0 4h2v2h-2v-2m4-4h2v2h-2v-2m0 4h2v2h-2v-2m-4-4h2v2h-2v-2m0 4h2v2h-2v-2m4-4h2v2h-2v-2m0 4h2v2h-2v-2"/></svg>
          QR
        </button>
      </div>
    </div>
  `;
  
  return card;
}

function showQRCode(server, port, secret) {
  const qrLink = `tg://proxy?server=${server}&port=${port}&secret=${secret}`;
  
  // Clear previous QR code
  const container = document.getElementById('qr-code-container');
  container.innerHTML = '';
  
  // Generate QR code using the library
  const qrcode = new QRCode(container, {
    text: qrLink,
    width: 256,
    height: 256,
    colorDark: getComputedStyle(document.documentElement).getPropertyValue('--md-sys-color-on-surface').trim(),
    colorLight: getComputedStyle(document.documentElement).getPropertyValue('--md-sys-color-surface').trim(),
    correctLevel: QRCode.CorrectLevel.M
  });
  
  openDialog('qr-dialog');
}
