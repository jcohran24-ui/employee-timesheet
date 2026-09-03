let deferredInstallPrompt = null;

function isIos() {
  return /iphone|ipad|ipod/i.test(window.navigator.userAgent);
}

function isStandalone() {
  return window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
}

function setupInstallUi() {
  const installButton = document.getElementById('install-app-btn');
  const iosHelp = document.getElementById('ios-install-help');
  const installedNote = document.getElementById('installed-note');

  if (isStandalone()) {
    if (installedNote) installedNote.classList.remove('d-none');
    if (installButton) installButton.classList.add('d-none');
    if (iosHelp) iosHelp.classList.add('d-none');
    return;
  }

  if (isIos() && iosHelp) {
    iosHelp.classList.remove('d-none');
  }

  window.addEventListener('beforeinstallprompt', (event) => {
    event.preventDefault();
    deferredInstallPrompt = event;
    if (installButton) installButton.classList.remove('d-none');
  });

  if (installButton) {
    installButton.addEventListener('click', async () => {
      if (!deferredInstallPrompt) return;
      deferredInstallPrompt.prompt();
      await deferredInstallPrompt.userChoice;
      deferredInstallPrompt = null;
      installButton.classList.add('d-none');
    });
  }

  window.addEventListener('appinstalled', () => {
    if (installButton) installButton.classList.add('d-none');
    if (installedNote) installedNote.classList.remove('d-none');
  });
}

if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/service-worker.js').catch((error) => {
      console.warn('Service worker registration failed:', error);
    });
  });
}

document.addEventListener('DOMContentLoaded', setupInstallUi);
