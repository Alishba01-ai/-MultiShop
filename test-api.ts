fetch("http://localhost:5000/api/search", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    query: "laptop",
    platforms: ["alibaba"]
  })
})
  .then((res) => res.json())
  .then(console.log)
  .catch(console.error);
