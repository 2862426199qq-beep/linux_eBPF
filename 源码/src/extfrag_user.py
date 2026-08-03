#!/usr/bin/env python3
import traceback 
import time
import curses
import sys
from extfrag import ExtFrag
from datetime import datetime


def generate_fragmentation_bar(score, max_length=20):
    """生成用于显示碎片化程度的条形图"""
    proportion = min(max(score, 0), 1)
    bar_length = int(proportion * max_length)
    return '#' * bar_length + '-' * (max_length - bar_length)
def createBar(height,width,y,x,title:str):
        winbar = curses.newwin(height,width,y,x)
        winbar.border(0)
        winbar.addstr(0,1,title)
        winbar.refresh()
        return winbar

def setProgress(win,progress):
        h,w = win.getmaxyx()
        char_max_w= w-3
        displayclear = "█"*char_max_w 
        win.addstr(1, 1, "{}".format(displayclear),curses.color_pair(1))
        rangex = (char_max_w / float(100)) * progress
        pos = int(rangex)
        res = 0
        if pos==0:
            res = 1
            pos+=1
        display = "█"*pos
        numstr=str(format(progress,'.1f'))
        win.addstr(0,w-9,"{}%".format(numstr)+" "*1)
        if  res==0:
            win.addstr(1, 1, "{}".format(display),curses.color_pair(2))
            win.refresh()
        else:
            win.refresh()

def screenEnough(screen):
    height, width = screen.getmaxyx()
    if height < 30 or width < 120:
        _boo = True
        screen.clear()
        errmsg = "[ERROR] Screen size is not enough!"
        screen.addstr(height // 2, abs(width - len(errmsg)) // 2, errmsg, curses.color_pair(2) | curses.A_BOLD)
        notemsg = "Resize your window and check the font size, or [Ctrl+C] to quit."
        screen.addstr(height // 2 + 1, abs(width - len(notemsg)) // 2, notemsg, curses.A_REVERSE)

        key = 0  
        while True:
            screen.refresh()
            height, width = screen.getmaxyx()
            if height >= 30 and width >= 120:
                _boo = False
                screen.clear()
                break
            try:
                key = screen.getch() 
                if key == curses.KEY_RESIZE: 
                    continue
            except KeyboardInterrupt:  # 捕获Ctrl+C
                curses.endwin()
                exit(0)

def main(screen):
    _boo = False
    _show=False
    last_update_time = time.time()
    curses.curs_set(0)  # 隐藏光标 
    screen.nodelay(True) 
    curses.noecho()
    curses.cbreak()
    screen.clear()
    curses.start_color()
    curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_BLACK)
    curses.init_pair(2, curses.COLOR_RED, curses.COLOR_BLACK)
    curses.init_pair(3, curses.COLOR_GREEN, curses.COLOR_BLACK)
    curses.init_pair(4, curses.COLOR_BLUE, curses.COLOR_BLACK)
    curses.init_pair(5, curses.COLOR_MAGENTA, curses.COLOR_BLACK)
    curses.init_pair(6, curses.COLOR_CYAN, curses.COLOR_BLACK)
    curses.init_pair(7, curses.COLOR_WHITE, curses.COLOR_BLACK)


    #? Argument parser ------------------------------------------------------------------------------->

    try:
        if len(sys.argv) > 1:
            args = sys.argv[1:]
            arg_count = len(args)
            i=0
            while i<arg_count:
                arg=args[i]
                if arg.startswith('-') and  arg not in ["-d", "-n", "-i", "-c", "-h", "--help", "-e", "-u", "-b", "-s", "-z","-v"]:
                    screen.clear()
                    height, width = screen.getmaxyx()
                    errmsg = f'[ERROR] Unrecognized argument: {arg}\n'
                    screen.addstr(height // 2, abs(width - len(errmsg)) // 2, errmsg, curses.color_pair(2) | curses.A_BOLD)
                    notemsg = "Use argument -h or --help for help"
                    screen.addstr(height // 2 + 1, abs(width - len(notemsg)) // 2, notemsg, curses.A_REVERSE)
                    screen.refresh()
                    time.sleep(100)
                elif arg=='-d':
                    if i + 1 >= arg_count or not args[i + 1].isdigit():
                        screen.clear()
                        height, width = screen.getmaxyx()
                        errmsg = f'[ERROR] Unrecognized argument: {arg}\n'
                        screen.addstr(height // 2, abs(width - len(errmsg)) // 2, errmsg, curses.color_pair(2) | curses.A_BOLD)
                        notemsg = "Use argument -h or --help for help"
                        screen.addstr(height // 2 + 1, abs(width - len(notemsg)) // 2, notemsg, curses.A_REVERSE)
                        screen.refresh()
                        time.sleep(100)
                    i+=1
                elif arg=='-i':
                    if i + 1 >= arg_count or not args[i + 1].isdigit():
                        screen.clear()
                        height, width = screen.getmaxyx()
                        errmsg = f'[ERROR] Unrecognized argument: {arg}\n'
                        screen.addstr(height // 2, abs(width - len(errmsg)) // 2, errmsg, curses.color_pair(2) | curses.A_BOLD)
                        notemsg = "Use argument -h or --help for help"
                        screen.addstr(height // 2 + 1, abs(width - len(notemsg)) // 2, notemsg, curses.A_REVERSE)
                        screen.refresh()
                        time.sleep(100)
                    i+=1 
                elif arg=='-c':
                    valid_options = ["Moveable", "DMA", "Normal","DMA32","Device"]
                    if i + 1 >= arg_count or args[i + 1] not in valid_options:
                        screen.clear()
                        height, width = screen.getmaxyx()
                        errmsg = f'[ERROR] Unrecognized argument: {arg}\n'
                        screen.addstr(height // 2, abs(width - len(errmsg)) // 2, errmsg, curses.color_pair(2) | curses.A_BOLD)
                        notemsg = "Use argument -h or --help for help"
                        screen.addstr(height // 2 + 1, abs(width - len(notemsg)) // 2, notemsg, curses.A_REVERSE)
                        screen.refresh()
                        time.sleep(100)
                    else:
                        i+=1
                i+=1

                
        if "-h" in sys.argv or "--help" in sys.argv:
            screenEnough(screen)
            if not _boo:
                screen.clear()
                header1 =f"Usage: {sys.argv[0]} [argument]\n\n"\
                f'Arguments:\n'\
                f'    -d, --delay           Delay between updates in seconds (default: 2)\n'\
                f'    -n, --node_info       Output node informations\n'\
                f'    -i, --node_id         Specify Node ID to get zone information\n'\
                f'    -c, --comm            Filter by zone_comm name\n'\
                f'    -e, --extfrag_index   Only output extfrag_index\n'\
                f'    -u, --unusable_index  Only output unusable_index\n'\
                f'    -s, --output_count    Output fragmentation count\n'\
                f'    -b, --bar             Display fragmentation bar\n'\
                f'    -z, --zone_info       Display detailed zone information\n'\
                f'    -v, --view            Display fragmentation figure\n'\
                f'    -h, --help            Show this help message and exit\n'
                msg = f"Please Crtl + C  exiting......\n\n"
                screen.addstr(0, 0, header1)
                screen.addstr(15, 0, msg)
                screen.refresh()
                time.sleep(100)
        else:
            # 解析参数
            args = {
                'delay': 2,  # 默认值
                'node_info': False,
                'node_id': None,
                'comm': None,
                'extfrag_index': False,
                'unusable_index': False,
                'output_count': False,
                'bar': False,
                'zone_info': False,
                'view':False
            }
            for i in range(1, len(sys.argv)):
                arg = sys.argv[i]
                if arg in ['-d', '--delay']:
                    args['delay'] = int(sys.argv[i + 1])
                elif arg in ['-n', '--node_info']:
                    args['node_info'] = True
                elif arg in ['-i', '--node_id']:
                    args['node_id'] = int(sys.argv[i + 1])
                elif arg in ['-c', '--comm']:
                    args['comm'] = sys.argv[i + 1]
                elif arg in ['-e', '--extfrag_index']:
                    args['extfrag_index'] = True
                elif arg in ['-u', '--unusable_index']:
                    args['unusable_index'] = True
                elif arg in ['-s', '--output_count']:
                    args['output_count'] = True
                elif arg in ['-b', '--bar']:
                    args['bar'] = True
                elif arg in ['-z', '--zone_info']:
                    args['zone_info'] = True
                elif arg in ['-v', '--view']:
                    args['view'] = True

            extfrag = ExtFrag(
            interval=args['delay'],
            output_count=args['output_count'],
            output_extfrag_index=args['extfrag_index'],
            output_unusable_index=args['unusable_index'],
            zone_info=args['zone_info'])
            screen.clear()
            while True:
                    # screen.clear()
                    row = 0
                    max_rows, max_cols = screen.getmaxyx()

                    if args['node_info']:
                        screenEnough(screen)
                        if not _boo:
                            # 获取并打印节点信息
                            node_data = extfrag.get_node_data()
                            if  node_data:
                                header = f"{'NODE_ID':>45} {'Number of Zones':>65} {'NODE_START_PFN':>70}\n"
                                screen.addstr(row, 0, header,curses.color_pair(4))
                                row += 1
                                for node_id, node in node_data.items():
                                    line = f"{node_id:>40} {node['nr_zones']:>60} {node['pgdat_ptr']:>77}\n"
                                    if row < max_rows - 1:  # 确保不超出屏幕行数
                                        if len(line) < max_cols - 1:  # 确保行内字符数不超出屏幕宽度
                                            screen.addstr(row, 0, line)
                                            row += 1
                                        else:
                                            screen.addstr(row, 0, line[:max_cols - 1])
                                            row += 1
                    elif args['output_count']:
                        screenEnough(screen)
                        if not _boo:
                            event_data = extfrag.get_count_data()
                            header = f"{'COMM':>25} {'PID':>30} {'PFN':>45}" \
                                    f"{'ALLOC_ORDER':>45} {'FALLBACK_ORDER':>45} {'COUNT':>35} \n"
                            screen.addstr(0, 0, header,curses.color_pair(4))
                            row =1
                            for event in event_data:
                                line = f"{event['pcomm']:>25} {event['pid']:>30} {event['pfn']:>45}{event['alloc_order']:>45} {event['fallback_order']:>45} {event['count']:>35} \n"
                                if row < max_rows - 1:  # 确保不超出屏幕行数
                                    if len(line) < max_cols - 1:  # 确保行内字符数不超出屏幕宽度
                                        screen.addstr(row, 0, line)
                                        row += 1
                                    else:
                                        screen.addstr(row, 0, line[:max_cols - 1])
                                        row += 1

                    elif args['zone_info']:
                        screenEnough(screen)
                        if not _boo:
                            # 获取并打印区域信息
                            if  args['node_id'] is not None:
                                zone_data = extfrag.get_zone_data(args['node_id'])
                            else:
                                zone_data = extfrag.get_zone_data()
                            if args['extfrag_index']:
                                header =f"{'ZONE_COMM':>5} {'ZONE_PFN':>15} {'SUM_PAGES':>20} {'FACT_PAGES':>20} " \
                                    f"{'ORDER':>15} {'TOTAL':>20} {'SUITABLE':>20} {'FREE':>20} {'NODE_ID':>20} {'extfrag_index':>25}"
                            elif args['unusable_index']:
                                header =  f"{'ZONE_COMM':>5} {'ZONE_PFN':>15} {'SUM_PAGES':>20} {'FACT_PAGES':>20} " \
                                    f"{'ORDER':>15} {'TOTAL':>20} {'SUITABLE':>20} {'FREE':>20} {'NODE_ID':>20} {'unusable_index':>25} "
                            else:
                                header = f"{'ZONE_COMM':>5} {'ZONE_PFN':>15} {'SUM_PAGES':>20} {'FACT_PAGES':>20} " \
                                    f"{'ORDER':>15} {'TOTAL':>20} {'SUITABLE':>20} {'FREE':>20} {'NODE_ID':>20} {'extfrag_index':>25} {'unusable_index':>20}"
                            if  args['bar']:
                                header+=f"{'BAR':>25}\n"
                            else:
                                header+="\n"
                            screen.addstr(0, 0, header,curses.color_pair(4))
                            row = 1
                            for comm, zones in zone_data.items():
                                if  args['comm'] and comm !=  args['comm']:
                                    continue   
                    
                                for zone in zones:
                                    color = curses.color_pair(3)
                                    if zone['order'] > 5 and float(zone['scoreB']) > 0.5:
                                        color = curses.color_pair(2)  # 红色，表示高风险
                                    if args['extfrag_index'] :
                                        line = f"{zone['comm']:^9} {zone['zone_pfn']:^20} {zone['spanned_pages']:^22} " \
                                        f"{zone['present_pages']:^18} {zone['order']:^15} {zone['free_blocks_total']:^25} " \
                                        f"{zone['free_blocks_suitable']:^15} {zone['free_pages']:^25} {zone['node_id']:^15} {zone['scoreA']:^20} "
                                    elif  args['unusable_index']:
                                        line = f"{zone['comm']:^9} {zone['zone_pfn']:^20} {zone['spanned_pages']:^22} " \
                                        f"{zone['present_pages']:^18} {zone['order']:^15} {zone['free_blocks_total']:^25} " \
                                        f"{zone['free_blocks_suitable']:^15} {zone['free_pages']:^25} {zone['node_id']:^15} {zone['scoreB']:^20} "
                                    else:
                                        line = f"{zone['comm']:^9} {zone['zone_pfn']:^20} {zone['spanned_pages']:^22} " \
                                        f"{zone['present_pages']:^18} {zone['order']:^15} {zone['free_blocks_total']:^25} " \
                                        f"{zone['free_blocks_suitable']:^15} {zone['free_pages']:^25} {zone['node_id']:^15} {zone['scoreA']:^20} {zone['scoreB']:^25}"
                                    if args['bar']:
                                        score = float(zone.get("scoreA" if args['extfrag_index'] else "scoreB", 0))
                                        frag_bar = generate_fragmentation_bar(score)
                                        line += f" {frag_bar:^40}\n" 
                                    else:
                                        line+="\n"
                                    if row < max_rows - 1:  # 确保不超出屏幕行数
                                        if len(line) < max_cols - 1:  # 确保行内字符数不超出屏幕宽度
                                            screen.addstr(row, 0, line,color)
                                            row += 1
                                        else:
                                            screen.addstr(row, 0, line[:max_cols - 1],color)
                                            row += 1
                            
                    elif args['view']:
                            # curses.initscr()
                            screenEnough(screen)
                            current_time = datetime.now().strftime('%Y--%m-%d：%H:%M:%S')
                            screen.addstr(0, 0, current_time)
                            screen.refresh()
                            current_time = time.time()
                            if current_time - last_update_time >= args['delay']:
                                _show = True
                                last_update_time=current_time
                            if not _boo and _show:
                                _show = False
                                # 获取并打印关键信息
                                if args['node_id'] is not None:
                                    view_data = extfrag.get_view_data(args['node_id'])
                                    zone_data = extfrag.get_zone_data(args['node_id'])
                                else:
                                    view_data = extfrag.get_view_data()
                                    zone_data = extfrag.get_zone_data()
                            

                                y_pos =2
                                row=3
                                bars = {}
                                current_progress = {}
                                for (node_id, comm), zones in view_data.items():
                                        if args['comm'] and comm != args['comm']:
                                            continue
                                        _str = f"Node {node_id}, zone {comm}   "
                                        screen.addstr(row, 0, _str)
                                        for i in range(11):  
                                            pbar = createBar(3, 21, y_pos, 24 + (i * 21), str(i))
                                            setProgress(pbar, 0) 
                                            bars[(node_id, comm, i)] = pbar   # 保存小窗口
                                            current_progress[(node_id, comm, i)] = 0  # 初始化为0
                                        y_pos+=3
                                        row+=3
                                for comm, zones in zone_data.items():
                                    if args['comm'] and comm != args['comm']:
                                            continue
                                    for zone in zones:
                                        node_id = zone['node_id']
                                        comm = zone['comm']
                                        order = zone['order']
                                        progress = zone['scoreB'].strip().split()[0]
                                        progress = float(progress) 
                                        key = (node_id, comm, order)
                                        if key in bars:
                                            if progress != current_progress[key]:
                                                current_progress[key] = progress  
                                                progress*=100
                                                setProgress(bars[key], progress)
                                                
                    else:
                        screenEnough(screen)
                        if not _boo:
                            # 获取并打印关键信息
                            if args['node_id'] is not None:
                                zone_data = extfrag.get_zone_data(args['node_id'])
                            else:
                                zone_data = extfrag.get_zone_data()
                            if args['extfrag_index']:
                                header =f"{'ZONE_COMM':<30}  {'NODE_ID':<23} {'ORDER':>40} {'extfrag_index':>50} "
                            elif args['unusable_index']:
                                header = f"{'ZONE_COMM':<30}  {'NODE_ID':<23} {'ORDER':>40}  {'unusable_index':>50} "
                            else:
                                header = f"{'ZONE_COMM':<30}  {'NODE_ID':<23} {'ORDER':>40} {'extfrag_index':>50} {'unusable_index':>30} "
                            if  args['bar']:
                                header+=f"{'BAR':>30}\n"
                            else:
                                header+="\n"
                            screen.addstr(row, 0, header,curses.color_pair(4))
                            row = 1
                            for comm, zones in zone_data.items():
                                if  args['comm'] and comm !=  args['comm']:
                                    continue  
                                for zone in zones:
                                    color = curses.color_pair(3)
                                    if zone['order'] > 5 and float(zone['scoreB']) > 0.5:
                                        color = curses.color_pair(2)  # 红色，表示高风险
                                    if args['extfrag_index'] :
                                        line = f"{zone['comm']:^7}  {zone['node_id']:^55}  {zone['order']:^55} {zone['scoreA']:^35} "
                                    elif  args['unusable_index']:
                                        line = f"{zone['comm']:^7}  {zone['node_id']:^55}  {zone['order']:^55} {zone['scoreB']:^35} "
                                    else:
                                        line = f"{zone['comm']:^7}  {zone['node_id']:^55}  {zone['order']:^55} {zone['scoreA']:^35}  {zone['scoreB']:^25} "
                                    if  args['bar']:
                                        score = float(zone.get("scoreA" if args['extfrag_index'] else "scoreB", 0))
                                        frag_bar = generate_fragmentation_bar(score)
                                        line += f" {frag_bar:^40}\n"  
                                    else:
                                        line+="\n"
                                    if row < max_rows - 1:  # 确保不超出屏幕行数
                                        if len(line) < max_cols - 1:  # 确保行内字符数不超出屏幕宽度
                                            screen.addstr(row, 0, line,color)
                                            row += 1
                                        else:
                                            screen.addstr(row, 0, line[:max_cols - 1],color)
                                            row += 1

                    if args['view']:
                        screen.refresh()
                    else:
                        screen.refresh()
                        time.sleep(args['delay'])
               

 
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    curses.wrapper(main)



