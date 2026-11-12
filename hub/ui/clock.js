// Ticks in the background, where page timers are slowed to about once a minute.
setInterval(() => postMessage(0), 3000);
