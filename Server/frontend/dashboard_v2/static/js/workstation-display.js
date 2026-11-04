window.WORKSTATION_DISPLAY_NAMES = window.WORKSTATION_DISPLAY_NAMES || {
  "192.168.4.202": "1650-1",
  "192.168.4.201": "1650-2",
  "192.168.4.203": "1650-2-1",
  "192.168.4.204": "1650-2-2",
  "192.168.4.205": "114-1",
  "192.168.4.206": "114-2"
};

window.getWorkstationDisplayName = window.getWorkstationDisplayName || function getWorkstationDisplayName(ip) {
  const displayName = window.WORKSTATION_DISPLAY_NAMES[ip];
  return displayName ? `工作站：${displayName}` : ip;
};

window.applyWorkstationDisplayNames = window.applyWorkstationDisplayNames || function applyWorkstationDisplayNames(root = document) {
  root.querySelectorAll('[data-workstation-display]').forEach((el) => {
    const ip = el.getAttribute('data-ip') || '';
    el.textContent = window.getWorkstationDisplayName(ip);
    if (ip && !el.getAttribute('title')) {
      el.setAttribute('title', ip);
    }
  });
};

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => window.applyWorkstationDisplayNames());
} else {
  window.applyWorkstationDisplayNames();
}
