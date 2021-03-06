import requests
import json
import re
import sys
from bs4 import BeautifulSoup
import execjs
import time as timelib
from bupt_gym_reserve import GymConfig, PageFormatException, CommandLineLoader, JsonLoader, SeverChanNotifier, ConfigException, merge_configs
from Crypto.Cipher import AES
import base64

req_config = {
    'headers': {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
        'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/87.04280.66 Safari/537.36',
        'accept': '*/*',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8'
    },
    'urls': {
        'index': 'https://gym.byr.moe/index.php',
        'login': 'https://gym.byr.moe/login.php',
        'order': 'https://gym.byr.moe/newOrder.php'
    }
}

error_reason = {
    '1': '预约成功',
    '2': '非法请求！',
    '3': '本时间段人数已满!',
    '4': '您已预约本时段健身房!',
    '5': '参数错误!请勿作死!',
    '6': '近两周内有不良预约记录!请反省',
    'dont spam': '服务器将访问识别为垃圾信息'
}

period_list = ('', '18:40 - 19:40', '19:40 - 20:40', '20:40 - 21:40')


class Reserve:
    def __init__(self, year: int, mon: int, day: int, period: int,
                 total: int, reserved: int, has_reserved: bool) -> None:
        self.year = year
        self.mon = mon
        self.day = day
        # 0, 1, 2
        self.period = period
        self.total = total
        self.reserved = reserved
        self.has_reserved = has_reserved
        self.reservable = (reserved < total) and not has_reserved

    def __str__(self) -> str:
        return f'''时间：{self.year}/{self.mon}/{self.day}\n时段：{period_list[self.period]}'''


class GymSession(requests.Session):
    def __init__(self, config: GymConfig) -> None:
        super().__init__()
        self.config = config
        # 最近一次登录结果
        self._login_flag = False
        # 尝试登录的次数
        self._login_attemption = 0
        self.cookie_path = config.cookie_path
        self.headers.update(req_config['headers'])
        self._recover_or_create_session()
        self.index_cache = None

    def save(self):
        print('正在保存session...')
        with open(self.cookie_path, 'w+') as foo:
            json.dump(self.cookies.get_dict(), foo)

    def has_login(self, force_request=False) -> bool:
        # 如果登录的flag为假 或者 用户目前没有尝试过登录
        # 则会请求访问index来检查是否登录
        # 否则返回缓存的login_flag
        if (self._login_attemption == 0 and not self._login_flag) or force_request:
            response_text = self.get(req_config['urls']['index']).text
            self._login_flag = '点击下方可用时间段进行预订' in response_text
            if self._login_flag:
                self.index_cache = response_text
        return self._login_flag

    def _recover_or_create_session(self):
        try:
            with open(self.cookie_path) as foo:
                # 存在cookie，直接从文件中加载
                cookies = json.load(foo)
                for k in cookies:
                    self.cookies.set(k, cookies[k])
                print('存在session，检测是否已经登录...')
                if self.has_login():
                    print('已经登录！')
                else:
                    print('用户未登录，正在尝试登录...')
                    self.cookies.clear()
                    self.login(self.config['username'], self.config['password'])
        except (json.decoder.JSONDecodeError, FileNotFoundError):
            # 不存在cookie，创建cookie并登录
            print('不存在session, 登录中...')
            self.login(self.config['username'], self.config['password'])

    # 返回登录成功状态
    def login(self, username: str, password: str) -> bool:
        if not username or not password:
            sys.stderr.write('用户名以及密码不能为空\n')
            sys.exit()
        payload = {
            'username': username,
            'password': password,
            'usertype': 0,
            'action': None
        }
        self.post(req_config['urls']['login'], data=payload)
        login_res = self.has_login()
        self._login_attemption += 1
        self._login_flag = login_res
        print('登录成功！'if login_res else '登录失败！请检查用户名/密码是否正确')
        return login_res


class Reserver:
    def __init__(self, config: GymConfig, session: GymSession) -> None:
        self.config = config
        self.session = session

    def get_reserves(self, index_cache: str = None) -> list:
        reservation_list = list()
        if index_cache is None:
            index_page = self.session.get(req_config['urls']['index']).text
        else:
            index_page = index_cache
        index_soup = BeautifulSoup(index_page, 'html.parser')
        lis = index_soup.select('.collapsible.popout>li')
        for li in lis:
            body = li.select_one('.collapsible-body')
            date = body['id']
            try:
                year = int(date[0:4])
                mon = int(date[4:6])
                day = int(date[6:len(date)])
            except ValueError:
                raise PageFormatException(msg=f'无法正确从文本 "{date}" 中获取预约日期')
            for i, time in enumerate(body.select('.timeBox')):
                right_text = time.select_one('.rightBox').get_text().strip()
                has_reserved = '已预约' in right_text
                # 寻找文本中出现的所有数字
                reserve_match = re.findall(r'\d+', right_text)
                if len(reserve_match) < 2:
                    raise PageFormatException(msg=f'无法正确从文本 "{right_text}" 中获取预约人数')
                reserved = reserve_match[0]
                total = reserve_match[1]
                reservation_list.append(Reserve(year, mon, day, i + 1, total, reserved, has_reserved))
        return reservation_list

    def reserve(self, reservable: Reserve) -> str:
        print(f'{reservable}')
        payload = {'blob': self._get_blob(reservable.year, reservable.mon,
                                          reservable.day, reservable.period, int(round(timelib.time()*1000)))}

        status = self.session.post(req_config['urls']['order'], data=payload).text.rstrip()
        if status == '1':
            print('预约成功！')
        elif status in error_reason:
            print(f'预约失败！失败理由：{error_reason[status]}')
        else:
            print(f'服务器异常，返回code：{status}')
        return status.rstrip()

    def reserve_all(self, reserve_list: list) -> tuple:
        success_list = list()
        fail_list = list()
        for i, r in enumerate(reserve_list):
            print(f'正在预约第{i+1}个...')
            suc = self.reserve(r)
            if suc == '1':
                success_list.append(r)
            else:
                fail_list.append((r, error_reason[suc] if suc in error_reason else '未知错误'))
            # 休眠1.5秒，以防服务器认定为trash
            timelib.sleep(1.5)

        return (success_list, fail_list)

    def _get_blob(self, year: int, mon: int, day: int, period: int, time: int):
        raw = execjs.eval('''JSON.stringify(
                    {{
                        date: '{}',
                        time: '{}',
                        timemill: {}
                    }}
                )'''.format('{:04d}{:02d}{:02d}'.format(year, mon, day), period, time))
        oraw = ''
        i = 0
        while i < len(raw):
            oraw += raw[i]+raw[len(raw)-1-i]
            i += 1

        def pad(m):
            return m+chr(16-len(m) % 16)*(16-len(m) % 16)
        oraw = pad(oraw).encode('utf-8')
        ekey = self.config['username'] * 2
        key = ekey[0:16].encode('utf-8')
        iv = ekey[2:18].encode('utf-8')
        cipher = AES.new(key, AES.MODE_CBC, iv)
        return base64.b64encode(bytes(cipher.encrypt(oraw)))


def roll_the_dice(chance: int) -> bool:
    if chance == 100:
        return True
    import random
    random.seed(int(10*timelib.time()+114514))
    res = random.randint(0, 100)
    return chance >= res


if __name__ == '__main__':
    print('********** {} **********'.format(
        timelib.strftime('%Y-%m-%d %H:%M:%S', timelib.localtime(timelib.time())))
    )
    command_line_loader = CommandLineLoader()
    config = command_line_loader.load_config()

    json_loader = JsonLoader(config_path=config.config_path)
    if not json_loader.load_status:
        json_config = None
        try:
            json_loader.load_config()
        except ConfigException as ce:
            sys.stderr.write(f'读取json配置文件出现错误：{ce}\n')
            sys.exit()
        merge_configs([config, json_config])

    print('正在摇D100... _(:з」∠)_')
    roll_result = roll_the_dice(config.chance)
    print('怎么又要干活o(￣ヘ￣o＃)' if roll_result else '好耶，是摸鱼时间！(๑•̀ㅂ•́)و✧')
    if not roll_result:
        sys.exit()

    notifier = SeverChanNotifier(sckey=config.sckey)
    session = GymSession(config=config)
    reserver = Reserver(config=config, session=session)
    reserves = list()
    try:
        reserves = reserver.get_reserves(index_cache=session.index_cache)
    except PageFormatException as e:
        sys.stderr.write(f'捕获到错误：{e.msg}\n')
        notifier.send_msg('预约出现错误', f'捕获到错误：{e.msg}')
        sys.exit()

    reservable = [_reserve for _reserve in reserves if _reserve.reservable]
    print(f'当前可预约有{len(reservable)} / {len(reserves)}个')
    if len(reservable) > 0:
        success_list, fail_list = reserver.reserve_all(reservable)
        if len(fail_list) != 0:
            print(f'失败{len(fail_list)}个，正在尝试重新预约')
            new_fail_list = list()
            for _reserve, _ in fail_list:
                new_fail_list.append(_reserve)
            sl, fl = reserver.reserve_all(new_fail_list)
            success_list += sl
            fail_list = fl
        if config.notify_enabled:
            title = f'成功预约{len(success_list)}个健身房时段，失败{len(fail_list)}个'
            content = '以下时段预约成功：'
            for suc in success_list:
                content += str(suc) + '   '
            content += '以下时段预约失败：'
            for failure, reason in fail_list:
                content += f'{str(failure)}+  失败原因：{reason}'
            if not notifier.send_msg(title, content):
                sys.stderr.write('推送消息至微信失败')
    else:
        print('无可用时段，退出中...')
    session.save()
    if json_loader.load_status:
        config.save()
        pass
