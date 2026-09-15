import { Moon, Sun } from "lucide-react";
import { useState } from "react";
import { applyTheme, persistTheme, type Theme } from "../lib/theme";
import { Button } from "./ui/button";

/**
 * 头部主题切换（票 12）：切 .dark class + localStorage 持久化。
 * 初始主题由 main.tsx 在首帧渲染前应用（避免闪错主题），此处从 <html> 现状反推。
 */
export function ThemeToggle() {
	const [theme, setTheme] = useState<Theme>(() =>
		document.documentElement.classList.contains("dark") ? "dark" : "light",
	);

	function toggle() {
		const next: Theme = theme === "dark" ? "light" : "dark";
		applyTheme(next);
		persistTheme(next);
		setTheme(next);
	}

	return (
		<Button
			variant="ghost"
			size="icon-sm"
			onClick={toggle}
			aria-label={theme === "dark" ? "切换到浅色主题" : "切换到暗色主题"}
			aria-pressed={theme === "dark"}
		>
			{theme === "dark" ? <Sun aria-hidden /> : <Moon aria-hidden />}
		</Button>
	);
}
