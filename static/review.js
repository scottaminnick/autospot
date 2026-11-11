'use strict';
document.querySelectorAll('.use-weather').forEach((button) => {
  button.addEventListener('click', () => {
    const input = document.getElementById(button.dataset.target);
    if (input) {
      input.value = button.dataset.value;
      input.focus();
      input.dispatchEvent(new Event('input', { bubbles: true }));
    }
  });
});

const approvedDownload = document.getElementById('approved-text-download');
if (window.location.hash === '#download-approved' && approvedDownload) {
  window.location.replace('#approved-forecast');
  approvedDownload.click();
}
