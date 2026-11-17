// Script of the placeholder page (the server's Content-Security-Policy forbids inline scripts).
const show = (id, value) => { document.getElementById(id).textContent = JSON.stringify(value, null, 2); };
fetch("/api/meta").then((r) => r.json()).then((meta) => show("meta", meta));
document.getElementById("form").addEventListener("submit", async (event) => {
  event.preventDefault();
  document.getElementById("result").textContent = "Running…";
  const body = Object.fromEntries(new FormData(event.target));
  try {
    const response = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    show("result", await response.json());
  } catch (error) {
    document.getElementById("result").textContent = String(error);
  }
});
