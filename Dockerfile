# Dockerfile

# 1. 选择一个合适的 Python 基础镜像
# python:3.11-slim 是一个不错的选择，体积较小
FROM python:3.11-slim

# 2. 设置工作目录
WORKDIR /app

# 3. 复制依赖文件到工作目录
COPY requirements.txt ./

# 4. 安装 Python 依赖
# --no-cache-dir 减少镜像大小
RUN pip install --no-cache-dir -r requirements.txt
# 如果需要处理SSL问题，可以取消注释并单独使用下面这行，或者合并到上面的RUN指令中，但不要和 COPY 混淆
# RUN pip install --no-cache-dir --trusted-host pypi.python.org --trusted-host files.pythonhosted.org --trusted-host pypi.org -r requirements.txt

# 5. 复制项目代码到工作目录
COPY . .
# 如果你的 templates 文件夹在项目根目录的子目录中，确保它也被正确复制
# 例如: COPY templates ./templates (如果 templates 在当前上下文的 templates/ 下)
# 但 COPY . . 已经会复制当前上下文的所有内容（包括 templates 文件夹，如果它存在于构建上下文中）

# 6. 暴露 Flask 应用运行的端口
EXPOSE 5000

# 7. 设置环境变量 (可选，但推荐)
ENV FLASK_APP=app.py
ENV FLASK_RUN_HOST=0.0.0.0
# 如果你的应用中使用了其他环境变量，也可以在这里设置

# 8. 定义容器启动时执行的命令
# 使用 gunicorn 作为生产环境的 WSGI 服务器是一个好选择
# 如果 gunicorn 不在 requirements.txt 中，需要先安装它
# (假设 gunicorn 已经在 requirements.txt 中)
# CMD ["gunicorn", "--workers", "1", "--bind", "0.0.0.0:5000","--timeout", "120", "--preload", "app:app"]

# 或者，如果只是为了简单运行，可以直接用 Flask 开发服务器 (不推荐用于生产)
# 你的 app.py 已经有了 app.run()，可以直接运行脚本
CMD ["python", "app.py"]