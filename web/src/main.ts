import { createApp } from "vue";
import App from "./App.vue";
import { desktop } from "./desktop";
import "./style.css";

if (desktop) document.documentElement.classList.add("desktop");

createApp(App).mount("#app");
