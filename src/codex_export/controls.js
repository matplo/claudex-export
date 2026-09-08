document.getElementById('expand')?.addEventListener('click', () => {
  document.querySelectorAll('details.tool').forEach(item => { item.open = true; });
});
document.getElementById('collapse')?.addEventListener('click', () => {
  document.querySelectorAll('details.tool').forEach(item => { item.open = false; });
});
document.getElementById('print')?.addEventListener('click', () => { window.print(); });
let previouslyOpen = [];
window.addEventListener('beforeprint', () => {
  previouslyOpen = [...document.querySelectorAll('details.tool')].map(item => item.open);
  document.querySelectorAll('details.tool').forEach(item => { item.open = true; });
});
window.addEventListener('afterprint', () => {
  document.querySelectorAll('details.tool').forEach((item, i) => { item.open = previouslyOpen[i]; });
});
