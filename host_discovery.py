from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, arp, ipv4, ether_types
from ryu.lib import hub
import time


class HostDiscoveryService(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    # Change this MAC if you want to block a different host in the demo.
    BLOCKED_MACS = {
        "00:00:00:00:00:04"
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mac_to_port = {}   # dpid -> {mac: port}
        self.hosts = {}         # mac -> host info
        self.monitor_thread = hub.spawn(self._monitor)

    def _monitor(self):
        while True:
            hub.sleep(10)
            self._print_host_db()

    def _print_host_db(self):
        self.logger.info("========== HOST DATABASE ==========")
        if not self.hosts:
            self.logger.info("No hosts learned yet.")
            return
        for mac, info in sorted(self.hosts.items()):
            self.logger.info(
                "MAC=%s IP=%s SW=%s PORT=%s LAST_SEEN=%.0f",
                mac, info.get("ip", "-"), info["dpid"], info["port"], info["last_seen"]
            )

    def _learn_host(self, dpid, port, mac, ip=None):
        now = time.time()
        old = self.hosts.get(mac)

        if old is None:
            self.logger.info(
                "HOST_JOIN: MAC=%s IP=%s on switch=%s port=%s",
                mac, ip or "-", dpid, port
            )
        else:
            moved = old["dpid"] != dpid or old["port"] != port
            if moved:
                self.logger.info(
                    "HOST_MOVE: MAC=%s from sw=%s/port=%s to sw=%s/port=%s",
                    mac, old["dpid"], old["port"], dpid, port
                )

        self.hosts[mac] = {
            "dpid": dpid,
            "port": port,
            "ip": ip or self.hosts.get(mac, {}).get("ip"),
            "last_seen": now,
        }

    def _add_flow(self, datapath, priority, match, actions=None, idle_timeout=0, hard_timeout=0):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        instructions = []
        if actions:
            instructions.append(parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions))

        mod = parser.OFPFlowMod(
            datapath=datapath,
            priority=priority,
            match=match,
            instructions=instructions,
            idle_timeout=idle_timeout,
            hard_timeout=hard_timeout,
        )
        datapath.send_msg(mod)

    def _install_block_rules(self, datapath):
        parser = datapath.ofproto_parser

        for mac in self.BLOCKED_MACS:
            # Drop any traffic sourced from the blocked host
            self._add_flow(
                datapath,
                priority=100,
                match=parser.OFPMatch(eth_src=mac),
                actions=[]
            )
            # Drop any traffic destined to the blocked host
            self._add_flow(
                datapath,
                priority=100,
                match=parser.OFPMatch(eth_dst=mac),
                actions=[]
            )

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # Table-miss => send unknown packets to controller
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
        self._add_flow(datapath, priority=0, match=match, actions=actions)

        # Install static drop rules for blocked MACs
        self._install_block_rules(datapath)

        self.logger.info("Switch %s connected and initialized.", datapath.id)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        dpid = datapath.id
        in_port = msg.match["in_port"]

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None:
            return

        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        src = eth.src
        dst = eth.dst

        # Learn the source host from any packet we receive
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        arp_pkt = pkt.get_protocol(arp.arp)

        if src in self.BLOCKED_MACS or dst in self.BLOCKED_MACS:
            self.logger.info("DROP: blocked MAC involved src=%s dst=%s", src, dst)
            return

        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][src] = in_port

        if arp_pkt:
            self._learn_host(dpid, in_port, src, arp_pkt.src_ip)
        elif ip_pkt:
            self._learn_host(dpid, in_port, src, ip_pkt.src)

        # Decide output port
        out_port = self.mac_to_port[dpid].get(dst, ofproto.OFPP_FLOOD)

        actions = [parser.OFPActionOutput(out_port)]

        # Install forwarding rule if destination is known
        if out_port != ofproto.OFPP_FLOOD:
            match = parser.OFPMatch(eth_dst=dst)
            self._add_flow(
                datapath=datapath,
                priority=10,
                match=match,
                actions=actions,
                idle_timeout=60,
                hard_timeout=300
            )
            self.logger.info("FLOW_INSTALLED: dpid=%s dst=%s -> port=%s", dpid, dst, out_port)

        # PacketOut
        data = None if msg.buffer_id != ofproto.OFP_NO_BUFFER else msg.data
        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=msg.buffer_id,
            in_port=in_port,
            actions=actions,
            data=data
        )
        datapath.send_msg(out)