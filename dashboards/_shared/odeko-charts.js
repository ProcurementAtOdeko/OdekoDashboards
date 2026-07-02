/* Odeko Chart.js defaults. Include after the Chart.js CDN script:
     <script src="../_shared/odeko-charts.js"></script> */
(function () {
  if (!window.Chart) return;
  Chart.defaults.color = "#8A7B6A";        /* --muted */
  Chart.defaults.borderColor = "#E6DFCC";  /* --line */
  Chart.defaults.font.family =
    '"Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif';
})();
