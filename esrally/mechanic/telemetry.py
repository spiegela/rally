import logging
import os
import re
import signal
import subprocess
import threading

import tabulate
from esrally import metrics, config
from esrally.utils import io, sysstats, process, console

logger = logging.getLogger("rally.telemetry")


def list_telemetry(cfg):
    console.println("Available telemetry devices:\n")
    console.println(tabulate.tabulate(Telemetry(cfg, devices=[
        JitCompiler(cfg, None),
        Gc(cfg, None),
        FlightRecorder(cfg, None),
        PerfStat(cfg,  None)
    ]).list(), ["Command", "Name", "Description"]))
    console.println("\nKeep in mind that each telemetry device may incur a runtime overhead which can skew results.")


class Telemetry:
    def __init__(self, cfg, devices):
        self.cfg = cfg
        self.devices = devices
        self.enabled_devices = self.cfg.opts("telemetry", "devices")

    def list(self):
        external_devices = []
        for device in self.devices:
            if not device.internal:
                external_devices.append([device.command, device.human_name, device.help])
        return external_devices

    def instrument_candidate_env(self, car, candidate_id):
        opts = {}
        for device in self.devices:
            if self._enabled(device):
                additional_opts = device.instrument_env(car, candidate_id)
                # properly merge values with the same key
                for k, v in additional_opts.items():
                    if k in opts:
                        opts[k] = "%s %s" % (opts[k], v)
                    else:
                        opts[k] = v
        return opts

    def attach_to_cluster(self, cluster):
        for device in self.devices:
            if self._enabled(device):
                device.attach_to_cluster(cluster)

    def attach_to_node(self, node):
        for device in self.devices:
            if self._enabled(device):
                device.attach_to_node(node)

    def detach_from_node(self, node):
        for device in self.devices:
            if self._enabled(device):
                device.detach_from_node(node)

    def on_benchmark_start(self):
        logger.info("Benchmark start")
        for device in self.devices:
            if self._enabled(device):
                device.on_benchmark_start()

    def on_benchmark_stop(self):
        logger.info("Benchmark stop")
        for device in self.devices:
            if self._enabled(device):
                device.on_benchmark_stop()

    def detach_from_cluster(self, cluster):
        for device in self.devices:
            if self._enabled(device):
                device.detach_from_cluster(cluster)

    def _enabled(self, device):
        return device.internal or device.command in self.enabled_devices


########################################################################################
#
# Telemetry devices
#
########################################################################################

class TelemetryDevice:
    def __init__(self, cfg, metrics_store):
        self.cfg = cfg
        self.metrics_store = metrics_store

    @property
    def internal(self):
        raise NotImplementedError("abstract method")

    @property
    def command(self):
        raise NotImplementedError("abstract method")

    @property
    def human_name(self):
        raise NotImplementedError("abstract method")

    @property
    def help(self):
        raise NotImplementedError("abstract method")

    def instrument_env(self, car, candidate_id):
        return {}

    def attach_to_cluster(self, cluster):
        pass

    def attach_to_node(self, node):
        pass

    def detach_from_node(self, node):
        pass

    def detach_from_cluster(self, cluster):
        pass

    def on_benchmark_start(self):
        pass

    def on_benchmark_stop(self):
        pass


class InternalTelemetryDevice(TelemetryDevice):
    def __init__(self, cfg, metrics_store):
        super().__init__(cfg, metrics_store)

    @property
    def internal(self):
        return True

    @property
    def command(self):
        return "internal"

    @property
    def human_name(self):
        return ""

    @property
    def help(self):
        return ""


class FlightRecorder(TelemetryDevice):
    def __init__(self, cfg, metrics_store):
        super().__init__(cfg, metrics_store)

    @property
    def internal(self):
        return False

    @property
    def command(self):
        return "jfr"

    @property
    def human_name(self):
        return "Flight Recorder"

    @property
    def help(self):
        return "Enables Java Flight Recorder (requires an Oracle JDK)"

    def instrument_env(self, car, candidate_id):
        log_root = "%s/%s" % (self.cfg.opts("system", "challenge.root.dir"), self.cfg.opts("benchmarks", "metrics.log.dir"))
        io.ensure_dir(log_root)
        log_file = "%s/%s-%s.jfr" % (log_root, car.name, candidate_id)

        console.info("%s: Writing flight recording to [%s]" % (self.human_name, log_file), logger=logger)
        # this is more robust in case we want to use custom settings
        # see http://stackoverflow.com/questions/34882035/how-to-record-allocations-with-jfr-on-command-line
        #
        # in that case change to: -XX:StartFlightRecording=defaultrecording=true,settings=es-memory-profiling
        return {"ES_JAVA_OPTS": "-XX:+UnlockDiagnosticVMOptions -XX:+UnlockCommercialFeatures -XX:+DebugNonSafepoints -XX:+FlightRecorder "
                                "-XX:FlightRecorderOptions=disk=true,maxage=0s,maxsize=0,dumponexit=true,dumponexitpath=%s "
                                "-XX:StartFlightRecording=defaultrecording=true" % log_file}


class JitCompiler(TelemetryDevice):
    def __init__(self, cfg, metrics_store):
        super().__init__(cfg, metrics_store)

    @property
    def internal(self):
        return False

    @property
    def command(self):
        return "jit"

    @property
    def human_name(self):
        return "JIT Compiler Profiler"

    @property
    def help(self):
        return "Enables JIT compiler logs."

    def instrument_env(self, car, candidate_id):
        log_root = "%s/%s" % (self.cfg.opts("system", "challenge.root.dir"), self.cfg.opts("benchmarks", "metrics.log.dir"))
        io.ensure_dir(log_root)
        log_file = "%s/%s-%s.jit.log" % (log_root, car.name, candidate_id)

        console.info("%s: Writing JIT compiler log to [%s]" % (self.human_name, log_file), logger=logger)
        return {"ES_JAVA_OPTS": "-XX:+UnlockDiagnosticVMOptions -XX:+TraceClassLoading -XX:+LogCompilation "
                                "-XX:LogFile=%s -XX:+PrintAssembly" % log_file}


class Gc(TelemetryDevice):
    def __init__(self, cfg, metrics_store):
        super().__init__(cfg, metrics_store)

    @property
    def internal(self):
        return False

    @property
    def command(self):
        return "gc"

    @property
    def human_name(self):
        return "GC log"

    @property
    def help(self):
        return "Enables GC logs."

    def instrument_env(self, car, candidate_id):
        log_root = "%s/%s" % (self.cfg.opts("system", "challenge.root.dir"), self.cfg.opts("benchmarks", "metrics.log.dir"))
        io.ensure_dir(log_root)
        log_file = "%s/%s-%s.gc.log" % (log_root, car.name, candidate_id)

        console.info("%s: Writing GC log to [%s]" % (self.human_name, log_file), logger=logger)
        # TODO dm: These options change in JDK 9!
        return {"ES_JAVA_OPTS": "-Xloggc:%s -XX:+PrintGCDetails -XX:+PrintGCDateStamps -XX:+PrintGCTimeStamps  "
                                "-XX:+PrintGCApplicationStoppedTime -XX:+PrintGCApplicationConcurrentTime  -XX:+PrintTenuringDistribution"
                                % log_file}


class PerfStat(TelemetryDevice):
    def __init__(self, cfg, metrics_store):
        super().__init__(cfg, metrics_store)
        self.process = None
        self.node = None
        self.log = None

    @property
    def internal(self):
        return False

    @property
    def command(self):
        return "perf"

    @property
    def human_name(self):
        return "perf stat"

    @property
    def help(self):
        return "Reads CPU PMU counters (requires Linux and perf)"

    def attach_to_node(self, node):
        log_root = "%s/%s" % (self.cfg.opts("system", "challenge.root.dir"), self.cfg.opts("benchmarks", "metrics.log.dir"))
        io.ensure_dir(log_root)
        log_file = "%s/%s.perf.log" % (log_root, node.node_name)

        console.info("%s: Writing perf logs to [%s]" % (self.human_name, log_file), logger=logger)

        self.log = open(log_file, "wb")

        self.process = subprocess.Popen(["perf", "stat", "-p %s" % node.process.pid],
                                        stdout=self.log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        self.node = node

    def detach_from_node(self, node):
        logger.info("Dumping PMU counters for node [%s]" % node.node_name)
        os.kill(self.process.pid, signal.SIGINT)
        try:
            self.process.wait(10.0)
        except subprocess.TimeoutExpired:
            logger.warn("perf stat did not terminate")
        self.log.close()


class MergeParts(InternalTelemetryDevice):
    """
    Gathers merge parts time statistics. Note that you need to run a track setup which logs these data.
    """
    MERGE_TIME_LINE = re.compile(r": (\d+) msec to merge ([a-z ]+) \[(\d+) docs\]")

    def __init__(self, cfg, metrics_store):
        super().__init__(cfg, metrics_store)
        self._t = None

    def on_benchmark_stop(self):
        server_log_dir = self.cfg.opts("launcher", "candidate.log.dir")
        for log_file in os.listdir(server_log_dir):
            log_path = "%s/%s" % (server_log_dir, log_file)
            logger.debug("Analyzing merge parts in [%s]" % log_path)
            with open(log_path) as f:
                merge_times = self._extract_merge_times(f)
                if merge_times:
                    self._store_merge_times(merge_times)

    def _extract_merge_times(self, file):
        merge_times = {}
        for line in file.readlines():
            match = MergeParts.MERGE_TIME_LINE.search(line)
            if match is not None:
                duration_ms, part, num_docs = match.groups()
                if part not in merge_times:
                    merge_times[part] = [0, 0]
                l = merge_times[part]
                l[0] += int(duration_ms)
                l[1] += int(num_docs)
        return merge_times

    def _store_merge_times(self, merge_times):
        for k, v in merge_times.items():
            metric_suffix = k.replace(" ", "_")
            # TODO dm: This is actually a node level metric (it is extracted from the *node's* log file), we have to add node info here)
            self.metrics_store.put_value_cluster_level("merge_parts_total_time_%s" % metric_suffix, v[0], "ms")
            self.metrics_store.put_count_cluster_level("merge_parts_total_docs_%s" % metric_suffix, v[1])


class DiskIo(InternalTelemetryDevice):
    """
    Gathers disk I/O stats.
    """
    def __init__(self, cfg, metrics_store):
        super().__init__(cfg, metrics_store)
        self.node = None
        self.process = None
        self.disk_start = None
        self.process_start = None

    def attach_to_node(self, node):
        self.node = node
        self.process = sysstats.setup_process_stats(node.process.pid)

    def on_benchmark_start(self):
        if self.process is not None:
            self.disk_start = sysstats.disk_io_counters()
            self.process_start = sysstats.process_io_counters(self.process)
            if self.process_start:
                logger.info("Using more accurate process-based I/O counters.")
            else:
                logger.warn("Process I/O counters are unsupported on this platform. Falling back to less accurate disk I/O counters.")

    def on_benchmark_stop(self):
        if self.process is not None:
            # Be aware the semantics of write counts etc. are different for disk and process statistics.
            # Thus we're conservative and only report I/O bytes now.
            disk_end = sysstats.disk_io_counters()
            process_end = sysstats.process_io_counters(self.process)
            self.metrics_store.put_count_node_level(self.node.node_name, "disk_io_write_bytes",
                                                    self.write_bytes(process_end, disk_end), "byte")
            self.metrics_store.put_count_node_level(self.node.node_name, "disk_io_read_bytes",
                                                    self.read_bytes(process_end, disk_end), "byte")

    def read_bytes(self, process_end, disk_end):
        if self.process_start and process_end:
            return process_end.read_bytes - self.process_start.read_bytes
        else:
            return disk_end.read_bytes - self.disk_start.read_bytes

    def write_bytes(self, process_end, disk_end):
        if self.process_start and process_end:
            return process_end.write_bytes - self.process_start.write_bytes
        else:
            return disk_end.write_bytes - self.disk_start.write_bytes


class CpuUsage(InternalTelemetryDevice):
    """
    Gathers CPU usage statistics.
    """
    def __init__(self, cfg, metrics_store):
        super().__init__(cfg, metrics_store)
        self.sampler = None
        self.node = None

    def attach_to_node(self, node):
        self.node = node

    def on_benchmark_start(self):
        if self.node:
            self.sampler = SampleCpuUsage(self.node, self.metrics_store)
            self.sampler.daemon = True
            self.sampler.start()

    def on_benchmark_stop(self):
        if self.sampler:
            self.sampler.finish()


class SampleCpuUsage(threading.Thread):
    def __init__(self, node, metrics_store):
        threading.Thread.__init__(self)
        self.stop = False
        self.node = node
        self.process = sysstats.setup_process_stats(node.process.pid)
        self.metrics_store = metrics_store

    def finish(self):
        self.stop = True
        self.join()

    def run(self):
        # noinspection PyBroadException
        try:
            while not self.stop:
                self.metrics_store.put_value_node_level(node_name=self.node.node_name, name="cpu_utilization_1s",
                                                        value=sysstats.cpu_utilization(self.process), unit="%")
        except BaseException:
            logger.exception("Could not determine CPU utilization")


def store_node_attribute_metadata(metrics_store, nodes_info):
    # push up all node level attributes to cluster level iff the values are identical for all nodes
    pseudo_cluster_attributes = {}
    for node in nodes_info:
        if "attributes" in node:
            for k, v in node["attributes"].items():
                attribute_key = "attribute_%s" % str(k)
                metrics_store.add_meta_info(metrics.MetaInfoScope.node, node["name"], attribute_key, v)
                if attribute_key not in pseudo_cluster_attributes:
                    pseudo_cluster_attributes[attribute_key] = set()
                pseudo_cluster_attributes[attribute_key].add(v)

    for k, v in pseudo_cluster_attributes.items():
        if len(v) == 1:
            metrics_store.add_meta_info(metrics.MetaInfoScope.cluster, None, k, next(iter(v)))


class EnvironmentInfo(InternalTelemetryDevice):
    """
    Gathers static environment information like OS or CPU details for Rally-provisioned clusters.
    """
    def __init__(self, cfg, client, metrics_store):
        super().__init__(cfg, metrics_store)
        self.client = client
        self._t = None

    def attach_to_cluster(self, cluster):
        revision = self.client.info()["version"]["build_hash"]
        distribution_version = self.client.info()["version"]["number"]
        self.metrics_store.add_meta_info(metrics.MetaInfoScope.cluster, None, "source_revision", revision)
        self.metrics_store.add_meta_info(metrics.MetaInfoScope.cluster, None, "distribution_version", distribution_version)
        self.cfg.add(config.Scope.benchmark, "meta", "source.revision", revision)
        self.cfg.add(config.Scope.benchmark, "source", "distribution.version", distribution_version)
        info = self.client.nodes.info(node_id="_all")
        nodes_info = info["nodes"].values()
        for node in nodes_info:
            node_name = node["name"]
            self.metrics_store.add_meta_info(metrics.MetaInfoScope.node, node_name, "jvm_vendor", node["jvm"]["vm_vendor"])
            self.metrics_store.add_meta_info(metrics.MetaInfoScope.node, node_name, "jvm_version", node["jvm"]["version"])

        store_node_attribute_metadata(self.metrics_store, nodes_info)

    def attach_to_node(self, node):
        # we gather also host level metrics here although they will just be overridden for multiple nodes on the same node (which is no
        # problem as the values are identical anyway).
        self.metrics_store.add_meta_info(metrics.MetaInfoScope.node, node.node_name, "os_name", sysstats.os_name())
        self.metrics_store.add_meta_info(metrics.MetaInfoScope.node, node.node_name, "os_version", sysstats.os_version())
        self.metrics_store.add_meta_info(metrics.MetaInfoScope.node, node.node_name, "cpu_logical_cores", sysstats.logical_cpu_cores())
        self.metrics_store.add_meta_info(metrics.MetaInfoScope.node, node.node_name, "cpu_physical_cores", sysstats.physical_cpu_cores())
        self.metrics_store.add_meta_info(metrics.MetaInfoScope.node, node.node_name, "cpu_model", sysstats.cpu_model())
        self.metrics_store.add_meta_info(metrics.MetaInfoScope.node, node.node_name, "node_name", node.node_name)
        # This is actually the only node level metric, but it is easier to implement this way
        self.metrics_store.add_meta_info(metrics.MetaInfoScope.node, node.node_name, "host_name", node.host_name)


class ExternalEnvironmentInfo(InternalTelemetryDevice):
    """
    Gathers static environment information for externally provisioned clusters.
    """
    def __init__(self, cfg, client, metrics_store):
        super().__init__(cfg, metrics_store)
        self.client = client
        self._t = None

    def attach_to_cluster(self, cluster):
        revision = self.client.info()["version"]["build_hash"]
        distribution_version = self.client.info()["version"]["number"]
        self.metrics_store.add_meta_info(metrics.MetaInfoScope.cluster, None, "source_revision", revision)
        self.metrics_store.add_meta_info(metrics.MetaInfoScope.cluster, None, "distribution_version", distribution_version)
        self.cfg.add(config.Scope.benchmark, "meta", "source.revision", revision)

        stats = self.client.nodes.stats(metric="_all")
        nodes = stats["nodes"]
        for node in nodes.values():
            node_name = node["name"]
            try:
                host = node["host"]
            except KeyError:
                host = "unknown"
            self.metrics_store.add_meta_info(metrics.MetaInfoScope.node, node_name, "node_name", node_name)
            self.metrics_store.add_meta_info(metrics.MetaInfoScope.node, node_name, "host_name", host)

        info = self.client.nodes.info(node_id="_all")
        nodes_info = info["nodes"].values()
        for node in nodes_info:
            self.try_store_node_info(node, "os_name", ["os", "name"])
            self.try_store_node_info(node, "os_version", ["os", "version"])
            self.try_store_node_info(node, "cpu_logical_cores", ["os", "available_processors"])
            self.try_store_node_info(node, "jvm_vendor", ["jvm", "vm_vendor"])
            self.try_store_node_info(node, "jvm_version", ["jvm", "version"])

        store_node_attribute_metadata(self.metrics_store, nodes_info)

    def try_store_node_info(self, node, metric_key, path):
        node_name = node["name"]
        value = node
        try:
            for k in path:
                value = value[k]
        except KeyError:
            logger.warn("Could not determine metric [%s] for node [%s] at path [%s]." % (metric_key, node_name, ",".join(path)))
            value = "unknown"
        self.metrics_store.add_meta_info(metrics.MetaInfoScope.node, node_name, metric_key, value)


class NodeStats(InternalTelemetryDevice):
    """
    Gathers statistics via the Elasticsearch nodes stats API
    """
    def __init__(self, cfg, client, metrics_store):
        super().__init__(cfg, metrics_store)
        self.client = client
        self.gc_times_per_node = {}

    def on_benchmark_start(self):
        self.gc_times_per_node = self.gc_times()

    def on_benchmark_stop(self):
        gc_times_at_end = self.gc_times()
        total_old_gen_collection_time = 0
        total_young_gen_collection_time = 0

        for node_name, gc_times_end in gc_times_at_end.items():
            if node_name in self.gc_times_per_node:
                gc_times_start = self.gc_times_per_node[node_name]
                young_gc_time = gc_times_end[0] - gc_times_start[0]
                old_gc_time = gc_times_end[1] - gc_times_start[1]

                total_young_gen_collection_time += young_gc_time
                total_old_gen_collection_time += old_gc_time

                self.metrics_store.put_value_node_level(node_name, "node_young_gen_gc_time", young_gc_time, "ms")
                self.metrics_store.put_value_node_level(node_name, "node_old_gen_gc_time", old_gc_time, "ms")
            else:
                logger.warn("Cannot determine GC times for node [%s]. It was not part of the cluster at the start of the benchmark.")

        self.metrics_store.put_value_cluster_level("node_total_young_gen_gc_time", total_young_gen_collection_time, "ms")
        self.metrics_store.put_value_cluster_level("node_total_old_gen_gc_time", total_old_gen_collection_time, "ms")

        self.gc_times_per_node = None

    def gc_times(self):
        logger.debug("Gathering GC times")
        gc_times = {}
        stats = self.client.nodes.stats(metric="_all")
        nodes = stats["nodes"]
        for node in nodes.values():
            node_name = node["name"]
            gc = node["jvm"]["gc"]["collectors"]
            old_gen_collection_time = gc["old"]["collection_time_in_millis"]
            young_gen_collection_time = gc["young"]["collection_time_in_millis"]
            gc_times[node_name] = (young_gen_collection_time, old_gen_collection_time)
        return gc_times


class IndexStats(InternalTelemetryDevice):
    """
    Gathers statistics via the Elasticsearch index stats API
    """
    def __init__(self, cfg, client, metrics_store):
        super().__init__(cfg, metrics_store)
        self.client = client

    def on_benchmark_stop(self):
        logger.info("Gathering indices stats")
        stats = self.client.indices.stats(metric="_all", level="shards", include_segment_file_sizes="true")
        p = stats["_all"]["primaries"]

        # actually this is add_count
        self.add_metrics(self.extract_value(p, ["segments", "count"]), "segments_count")
        self.add_metrics(self.extract_value(p, ["segments", "memory_in_bytes"]), "segments_memory_in_bytes", "byte")

        self.add_metrics(self.extract_value(p, ["segments", "doc_values_memory_in_bytes"]), "segments_doc_values_memory_in_bytes", "byte")
        self.add_metrics(self.extract_value(p, ["segments", "stored_fields_memory_in_bytes"]), "segments_stored_fields_memory_in_bytes", "byte")
        self.add_metrics(self.extract_value(p, ["segments", "terms_memory_in_bytes"]), "segments_terms_memory_in_bytes", "byte")
        self.add_metrics(self.extract_value(p, ["segments", "norms_memory_in_bytes"]), "segments_norms_memory_in_bytes", "byte")
        self.add_metrics(self.extract_value(p, ["segments", "points_memory_in_bytes"]), "segments_points_memory_in_bytes", "byte")

        self.add_metrics(self.extract_value(p, ["merges", "total_time_in_millis"]), "merges_total_time", "ms")
        self.add_metrics(self.extract_value(p, ["merges", "total_throttled_time_in_millis"]), "merges_total_throttled_time", "ms")
        self.add_metrics(self.extract_value(p, ["indexing", "index_time_in_millis"]), "indexing_total_time", "ms")
        self.add_metrics(self.extract_value(p, ["refresh", "total_time_in_millis"]), "refresh_total_time", "ms")
        self.add_metrics(self.extract_value(p, ["flush", "total_time_in_millis"]), "flush_total_time", "ms")

        self.add_metrics(self.extract_value(p, ["segments", "file_sizes", "dii", "size_in_bytes"]), "dii_size_in_bytes", "byte")
        self.add_metrics(self.extract_value(p, ["segments", "file_sizes", "doc", "size_in_bytes"]), "doc_size_in_bytes", "byte")
        self.add_metrics(self.extract_value(p, ["segments", "file_sizes", "fdx", "size_in_bytes"]), "fdx_size_in_bytes", "byte")
        self.add_metrics(self.extract_value(p, ["segments", "file_sizes", "dim", "size_in_bytes"]), "dim_size_in_bytes", "byte")
        self.add_metrics(self.extract_value(p, ["segments", "file_sizes", "fdt", "size_in_bytes"]), "fdt_size_in_bytes", "byte")
        self.add_metrics(self.extract_value(p, ["segments", "file_sizes", "fnm", "size_in_bytes"]), "fnm_size_in_bytes", "byte")
        self.add_metrics(self.extract_value(p, ["segments", "file_sizes", "dvd", "size_in_bytes"]), "dvd_size_in_bytes", "byte")
        self.add_metrics(self.extract_value(p, ["segments", "file_sizes", "dvm", "size_in_bytes"]), "dvm_size_in_bytes", "byte")
        self.add_metrics(self.extract_value(p, ["segments", "file_sizes", "tip", "size_in_bytes"]), "tip_size_in_bytes", "byte")
        self.add_metrics(self.extract_value(p, ["segments", "file_sizes", "tim", "size_in_bytes"]), "tim_size_in_bytes", "byte")
        self.add_metrics(self.extract_value(p, ["segments", "file_sizes", "si", "size_in_bytes"]), "si_size_in_bytes", "byte")
        self.add_metrics(self.extract_value(p, ["segments", "file_sizes", "nvd", "size_in_bytes"]), "nvd_size_in_bytes", "byte")
        self.add_metrics(self.extract_value(p, ["segments", "file_sizes", "nvm", "size_in_bytes"]), "nvm_size_in_bytes", "byte")
        self.add_metrics(self.extract_value(p, ["segments", "file_sizes", "pos", "size_in_bytes"]), "pos_size_in_bytes", "byte")

    def add_metrics(self, value, metric_key, unit=None):
        if value:
            if unit:
                self.metrics_store.put_value_cluster_level(metric_key, value, unit)
            else:
                self.metrics_store.put_count_cluster_level(metric_key, value)

    def extract_value(self, primaries, path):
        value = primaries
        try:
            for k in path:
                value = value[k]
            return value
        except KeyError:
            logger.warn("Could not determine value at path [%s]." % ",".join(path))
            return None


class IndexSize(InternalTelemetryDevice):
    """
    Measures the final size of the index
    """
    def __init__(self, cfg, metrics_store):
        super().__init__(cfg, metrics_store)

    def detach_from_cluster(self, cluster):
        data_paths = self.cfg.opts("provisioning", "local.data.paths", mandatory=False)
        if data_paths is not None:
            data_path = data_paths[0]
            index_size_bytes = io.get_size(data_path)
            self.metrics_store.put_count_cluster_level("final_index_size_bytes", index_size_bytes, "byte")
            process.run_subprocess_with_logging("find %s -ls" % data_path, header="index files:")
