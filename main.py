import subprocess
import asyncio
import sys
import signal

# ДОБАВЛЕН telethon_worker.py
scripts = ["bot.py", "parser.py", "telethon_worker.py"]

def run_script(script_name):
    """Запуск скрипта и возврат процесса"""
    print(f"🚀 Запуск {script_name}...")
    return subprocess.Popen([sys.executable, script_name])

async def monitor_processes():
    """Асинхронный мониторинг процессов с перезапуском"""
    processes = {script: run_script(script) for script in scripts}
    
    # ДОБАВЛЕНО: правильная обработка Ctrl+C
    loop = asyncio.get_running_loop()
    for sig in [signal.SIGINT, signal.SIGTERM]:
        loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown(processes)))
    
    try:
        while True:
            for script, process in list(processes.items()):
                # Если процесс завершился, значит он упал
                if process.poll() is not None:
                    print(f"⚠️ {script} упал (код {process.returncode}). Перезапуск через 5 секунд...")
                    await asyncio.sleep(5)
                    processes[script] = run_script(script)
                    print(f"✅ {script} перезапущен")
            
            # ИСПРАВЛЕНО: асинхронная задержка вместо time.sleep()
            await asyncio.sleep(10)
            
    except asyncio.CancelledError:
        print("\n🛑 Остановка монитора...")
        await shutdown(processes)

async def shutdown(processes):
    """Корректное завершение всех процессов"""
    print("🛑 Завершение всех процессов...")
    for script, process in processes.items():
        print(f"⏹️ Останавливаем {script}...")
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            print(f"⚠️ {script} не отвечает, принудительно завершаем...")
            process.kill()
    print("✅ Все процессы остановлены")
    sys.exit(0)

async def main():
    print("=" * 50)
    print("🚀 МОНИТОР ЗАПУЩЕН")
    print(f"📋 Отслеживаемые скрипты: {', '.join(scripts)}")
    print("=" * 50)
    
    await monitor_processes()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Программа остановлена пользователем")
        sys.exit(0)
    except Exception as e:
        print(f"💥 Критическая ошибка: {e}")
        sys.exit(1)