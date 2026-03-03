#!/bin/bash

# 设置颜色输出
GREEN='\033[0;32m'
NC='\033[0m' # No Color

echo -e "${GREEN}开始配置 Mac 开发环境...${NC}"

# 1. 创建 bin 目录
echo "正在检查 bin 目录..."
if [ ! -d "bin" ]; then
    mkdir bin
    echo "已创建 bin 目录"
else
    echo "bin 目录已存在"
fi

# 2. 检查并配置 FFmpeg
echo "正在配置 FFmpeg..."

# 尝试从系统路径复制（如果存在），否则下载
# 这里为了确保打包时的独立性，我们优先检查 bin 下是否有文件
if [ -f "bin/ffmpeg" ] && [ -f "bin/ffprobe" ]; then
    echo -e "${GREEN}FFmpeg 二进制文件已存在于 bin 目录，跳过下载。${NC}"
else
    # 检查系统是否安装了 ffmpeg
    if command -v ffmpeg &> /dev/null; then
        echo "检测到系统已安装 FFmpeg，正在创建符号链接到 bin 目录..."
        # 获取系统 ffmpeg 路径
        SYS_FFMPEG=$(which ffmpeg)
        SYS_FFPROBE=$(which ffprobe)
        
        # 复制文件到 bin 目录（为了打包时方便，建议复制而不是软链，虽然软链开发能用，但打包会麻烦）
        # 但是系统安装的可能是动态链接的，复制过去可能跑不起来（依赖库问题）。
        # 所以最稳妥的还是下载静态编译版本。
        
        echo "为了确保打包后的程序能在其他 Mac 上运行，我们将下载静态编译版本的 FFmpeg。"
        echo "正在从 evermeet.cx 下载 FFmpeg (这可能需要一点时间)..."
        
        # 下载 ffmpeg
        curl -L -o ffmpeg.zip https://evermeet.cx/ffmpeg/ffmpeg-6.0.zip
        # 下载 ffprobe
        curl -L -o ffprobe.zip https://evermeet.cx/ffmpeg/ffprobe-6.0.zip
        
        echo "正在解压..."
        unzip -o -q ffmpeg.zip -d bin/
        unzip -o -q ffprobe.zip -d bin/
        
        # 清理压缩包
        rm ffmpeg.zip ffprobe.zip
        
        # 赋予执行权限
        chmod +x bin/ffmpeg
        chmod +x bin/ffprobe
        
        echo -e "${GREEN}FFmpeg 下载并配置完成！${NC}"
    else
        echo "系统未检测到 FFmpeg，正在下载静态编译版本..."
        # 同上下载逻辑
        curl -L -o ffmpeg.zip https://evermeet.cx/ffmpeg/ffmpeg-6.0.zip
        curl -L -o ffprobe.zip https://evermeet.cx/ffmpeg/ffprobe-6.0.zip
        unzip -o -q ffmpeg.zip -d bin/
        unzip -o -q ffprobe.zip -d bin/
        rm ffmpeg.zip ffprobe.zip
        chmod +x bin/ffmpeg
        chmod +x bin/ffprobe
        echo -e "${GREEN}FFmpeg 下载并配置完成！${NC}"
    fi
fi

# 3. 验证环境
echo "----------------------------------------"
echo "环境验证："
if [ -f "bin/ffmpeg" ]; then
    ./bin/ffmpeg -version | head -n 1
else
    echo "错误：bin/ffmpeg 未找到"
fi

echo "----------------------------------------"
echo -e "${GREEN}环境配置完毕！${NC}"
chmod +x start.command
echo "现在你可以："
echo "1. 直接双击 ${GREEN}start.command${NC} 运行软件（推荐）"
echo "2. 或者在终端运行：${GREEN}./start.command${NC}"
