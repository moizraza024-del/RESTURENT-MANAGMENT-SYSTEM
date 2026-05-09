document.addEventListener('DOMContentLoaded', function() {
  const counters = document.querySelectorAll('.counter');
  counters.forEach(counter => {
    const target = +counter.getAttribute('data-target');
    let current = 0;
    const step = target / 80;
    const update = () => {
      current += step;
      if (current >= target) {
        counter.textContent = target;
      } else {
        counter.textContent = Math.round(current);
        requestAnimationFrame(update);
      }
    };
    update();
  });

  const observer = new IntersectionObserver(entries => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        entry.target.classList.add('visible');
      }
    });
  }, { threshold: 0.2 });

  document.querySelectorAll('.fade-on-scroll').forEach(el => observer.observe(el));
});