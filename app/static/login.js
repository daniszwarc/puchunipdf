document.getElementById("login").onsubmit = async (e) => {
  e.preventDefault();
  const err = document.getElementById("err");
  err.textContent = "";
  const r = await fetch("/api/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username: u.value, password: p.value }),
  });
  if (r.ok) return (location.href = "/");
  err.textContent = r.status === 429 ? "Too many attempts. Try again in 15 minutes." : "Invalid credentials";
  p.value = "";
};
