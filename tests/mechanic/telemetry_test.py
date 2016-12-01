import collections
import unittest.mock as mock
from unittest import TestCase

from esrally import config, metrics
from esrally.mechanic import telemetry, car, cluster


def create_config():
    cfg = config.Config()
    cfg.add(config.Scope.application, "system", "env.name", "unittest")
    cfg.add(config.Scope.application, "reporting", "datastore.host", "localhost")
    cfg.add(config.Scope.application, "reporting", "datastore.port", "0")
    cfg.add(config.Scope.application, "reporting", "datastore.secure", False)
    cfg.add(config.Scope.application, "reporting", "datastore.user", "")
    cfg.add(config.Scope.application, "reporting", "datastore.password", "")
    # only internal devices are active
    cfg.add(config.Scope.application, "telemetry", "devices", [])
    return cfg


class MockTelemetryDevice(telemetry.InternalTelemetryDevice):
    def __init__(self, cfg, metrics_store, mock_env):
        super().__init__(cfg, metrics_store)
        self.mock_env = mock_env

    def instrument_env(self, car, candidate_id):
        return self.mock_env


class TelemetryTests(TestCase):
    def test_merges_options_set_by_different_devices(self):
        cfg = config.Config()
        cfg.add(config.Scope.application, "telemetry", "devices", "jfr")
        cfg.add(config.Scope.application, "system", "challenge.root.dir", "challenge-root")
        cfg.add(config.Scope.application, "benchmarks", "metrics.log.dir", "telemetry")

        # we don't need one for this test
        metrics_store = None

        devices = [
            MockTelemetryDevice(cfg, metrics_store, {"ES_JAVA_OPTS": "-Xms256M"}),
            MockTelemetryDevice(cfg, metrics_store, {"ES_JAVA_OPTS": "-Xmx512M"}),
            MockTelemetryDevice(cfg, metrics_store, {"ES_NET_HOST": "127.0.0.1"})
        ]

        t = telemetry.Telemetry(cfg=cfg, devices=devices)

        default_car = car.Car(name="default-car")
        opts = t.instrument_candidate_env(default_car, "default-node")

        self.assertTrue(opts)
        self.assertEqual(len(opts), 2)
        self.assertEqual("-Xms256M -Xmx512M", opts["ES_JAVA_OPTS"])
        self.assertEqual("127.0.0.1", opts["ES_NET_HOST"])


class MergePartsDeviceTests(TestCase):
    def setUp(self):
        self.cfg = create_config()
        self.cfg.add(config.Scope.application, "launcher", "candidate.log.dir", "/unittests/var/log/elasticsearch")

    @mock.patch("esrally.metrics.EsMetricsStore.put_count_cluster_level")
    @mock.patch("esrally.metrics.EsMetricsStore.put_value_cluster_level")
    @mock.patch("builtins.open")
    @mock.patch("os.listdir")
    def test_store_nothing_if_no_metrics_present(self, listdir_mock, open_mock, metrics_store_put_value, metrics_store_put_count):
        listdir_mock.return_value = [open_mock]
        open_mock.side_effect = [
            mock.mock_open(read_data="no data to parse").return_value
        ]
        metrics_store = metrics.EsMetricsStore(self.cfg)
        merge_parts_device = telemetry.MergeParts(self.cfg, metrics_store)
        merge_parts_device.on_benchmark_stop()

        metrics_store_put_value.assert_not_called()
        metrics_store_put_count.assert_not_called()

    @mock.patch("esrally.metrics.EsMetricsStore.put_count_cluster_level")
    @mock.patch("esrally.metrics.EsMetricsStore.put_value_cluster_level")
    @mock.patch("builtins.open")
    @mock.patch("os.listdir")
    def test_store_calculated_metrics(self, listdir_mock, open_mock, metrics_store_put_value, metrics_store_put_count):
        log_file = '''
        INFO: System starting up
        INFO: 100 msec to merge doc values [500 docs]
        INFO: Something unrelated
        INFO: 250 msec to merge doc values [1350 docs]
        INFO: System shutting down
        '''
        listdir_mock.return_value = [open_mock]
        open_mock.side_effect = [
            mock.mock_open(read_data=log_file).return_value
        ]
        metrics_store = metrics.EsMetricsStore(self.cfg)
        merge_parts_device = telemetry.MergeParts(self.cfg, metrics_store)
        merge_parts_device.on_benchmark_stop()

        metrics_store_put_value.assert_called_with("merge_parts_total_time_doc_values", 350, "ms")
        metrics_store_put_count.assert_called_with("merge_parts_total_docs_doc_values", 1850)


class Client:
    def __init__(self, cluster=None, nodes=None, info=None, indices=None):
        self.cluster = cluster
        self.nodes = nodes
        self._info = info
        self.indices = indices

    def info(self):
        return self._info


class SubClient:
    def __init__(self, info):
        self._info = info

    def stats(self, *args, **kwargs):
        return self._info

    def info(self, *args, **kwargs):
        return self._info


class EnvironmentInfoTests(TestCase):
    def setUp(self):
        self.cfg = create_config()

    @mock.patch("esrally.metrics.EsMetricsStore.add_meta_info")
    def test_stores_cluster_level_metrics_on_attach(self, metrics_store_add_meta_info):
        nodes_info = {"nodes": collections.OrderedDict()}
        nodes_info["nodes"]["FCFjozkeTiOpN-SI88YEcg"] = {
            "name": "rally0",
            "host": "127.0.0.1",
            "attributes": {
                "group": "cold_nodes"
            },
            "os": {
                "name": "Mac OS X",
                "version": "10.11.4",
                "available_processors": 8
            },
            "jvm": {
                "version": "1.8.0_74",
                "vm_vendor": "Oracle Corporation"
            }
        }
        nodes_info["nodes"]["EEEjozkeTiOpN-SI88YEcg"] = {
            "name": "rally1",
            "host": "127.0.0.1",
            "attributes": {
                "group": "hot_nodes"
            },
            "os": {
                "name": "Mac OS X",
                "version": "10.11.5",
                "available_processors": 8
            },
            "jvm": {
                "version": "1.8.0_102",
                "vm_vendor": "Oracle Corporation"
            }
        }

        cluster_info = {
            "version":
                {
                    "build_hash": "abc123",
                    "number": "6.0.0-alpha1"
                }
        }

        client = Client(nodes=SubClient(nodes_info), info=cluster_info)
        metrics_store = metrics.EsMetricsStore(self.cfg)
        env_device = telemetry.EnvironmentInfo(self.cfg, client, metrics_store)
        t = telemetry.Telemetry(self.cfg, devices=[env_device])
        t.attach_to_cluster(cluster.Cluster([], t))
        calls = [
            mock.call(metrics.MetaInfoScope.cluster, None, "source_revision", "abc123"),
            mock.call(metrics.MetaInfoScope.cluster, None, "distribution_version", "6.0.0-alpha1"),
            mock.call(metrics.MetaInfoScope.node, "rally0", "jvm_vendor", "Oracle Corporation"),
            mock.call(metrics.MetaInfoScope.node, "rally0", "jvm_version", "1.8.0_74"),
            mock.call(metrics.MetaInfoScope.node, "rally1", "jvm_vendor", "Oracle Corporation"),
            mock.call(metrics.MetaInfoScope.node, "rally1", "jvm_version", "1.8.0_102"),
            mock.call(metrics.MetaInfoScope.node, "rally0", "attribute_group", "cold_nodes"),
            mock.call(metrics.MetaInfoScope.node, "rally1", "attribute_group", "hot_nodes")
        ]

        metrics_store_add_meta_info.assert_has_calls(calls)

    @mock.patch("esrally.metrics.EsMetricsStore.add_meta_info")
    @mock.patch("esrally.utils.sysstats.os_name")
    @mock.patch("esrally.utils.sysstats.os_version")
    @mock.patch("esrally.utils.sysstats.logical_cpu_cores")
    @mock.patch("esrally.utils.sysstats.physical_cpu_cores")
    @mock.patch("esrally.utils.sysstats.cpu_model")
    def test_stores_node_level_metrics_on_attach(self, cpu_model, physical_cpu_cores, logical_cpu_cores, os_version, os_name,
                                                 metrics_store_add_meta_info):
        cpu_model.return_value = "Intel(R) Core(TM) i7-4870HQ CPU @ 2.50GHz"
        physical_cpu_cores.return_value = 4
        logical_cpu_cores.return_value = 8
        os_version.return_value = "4.2.0-18-generic"
        os_name.return_value = "Linux"

        metrics_store = metrics.EsMetricsStore(self.cfg)
        node = cluster.Node(None, "io", "rally0", None)
        env_device = telemetry.EnvironmentInfo(self.cfg, None, metrics_store)
        env_device.attach_to_node(node)

        calls = [
            mock.call(metrics.MetaInfoScope.node, "rally0", "os_name", "Linux"),
            mock.call(metrics.MetaInfoScope.node, "rally0", "os_version", "4.2.0-18-generic"),
            mock.call(metrics.MetaInfoScope.node, "rally0", "cpu_logical_cores", 8),
            mock.call(metrics.MetaInfoScope.node, "rally0", "cpu_physical_cores", 4),
            mock.call(metrics.MetaInfoScope.node, "rally0", "cpu_model", "Intel(R) Core(TM) i7-4870HQ CPU @ 2.50GHz"),
            mock.call(metrics.MetaInfoScope.node, "rally0", "node_name", "rally0"),
            mock.call(metrics.MetaInfoScope.node, "rally0", "host_name", "io"),
        ]

        metrics_store_add_meta_info.assert_has_calls(calls)


class ExternalEnvironmentInfoTests(TestCase):
    def setUp(self):
        self.cfg = create_config()

    @mock.patch("esrally.metrics.EsMetricsStore.add_meta_info")
    def test_stores_cluster_level_metrics_on_attach(self, metrics_store_add_meta_info):
        nodes_stats = {
            "nodes": {
                "FCFjozkeTiOpN-SI88YEcg": {
                    "name": "rally0",
                    "host": "127.0.0.1"
                }
            }
        }

        nodes_info = {
            "nodes": {
                "FCFjozkeTiOpN-SI88YEcg": {
                    "name": "rally0",
                    "host": "127.0.0.1",
                    "attributes": {
                        "az": "us_east1"
                    },
                    "os": {
                        "name": "Mac OS X",
                        "version": "10.11.4",
                        "available_processors": 8
                    },
                    "jvm": {
                        "version": "1.8.0_74",
                        "vm_vendor": "Oracle Corporation"
                    }
                }
            }
        }
        cluster_info = {
            "version":
                {
                    "build_hash": "253032b",
                    "number": "5.0.0"

                }
        }
        client = Client(cluster=SubClient(nodes_stats), nodes=SubClient(nodes_info), info=cluster_info)
        metrics_store = metrics.EsMetricsStore(self.cfg)
        env_device = telemetry.ExternalEnvironmentInfo(self.cfg, client, metrics_store)
        t = telemetry.Telemetry(self.cfg, devices=[env_device])
        t.attach_to_cluster(cluster.Cluster([], t))

        calls = [
            mock.call(metrics.MetaInfoScope.cluster, None, "source_revision", "253032b"),
            mock.call(metrics.MetaInfoScope.cluster, None, "distribution_version", "5.0.0"),
            mock.call(metrics.MetaInfoScope.node, "rally0", "node_name", "rally0"),
            mock.call(metrics.MetaInfoScope.node, "rally0", "host_name", "127.0.0.1"),
            mock.call(metrics.MetaInfoScope.node, "rally0", "os_name", "Mac OS X"),
            mock.call(metrics.MetaInfoScope.node, "rally0", "os_version", "10.11.4"),
            mock.call(metrics.MetaInfoScope.node, "rally0", "cpu_logical_cores", 8),
            mock.call(metrics.MetaInfoScope.node, "rally0", "jvm_vendor", "Oracle Corporation"),
            mock.call(metrics.MetaInfoScope.node, "rally0", "jvm_version", "1.8.0_74"),
            mock.call(metrics.MetaInfoScope.node, "rally0", "attribute_az", "us_east1"),
            mock.call(metrics.MetaInfoScope.cluster, None, "attribute_az", "us_east1")
        ]
        metrics_store_add_meta_info.assert_has_calls(calls)

    @mock.patch("esrally.metrics.EsMetricsStore.add_meta_info")
    def test_fallback_when_host_not_available(self, metrics_store_add_meta_info):
        nodes_stats = {
            "nodes": {
                "FCFjozkeTiOpN-SI88YEcg": {
                    "name": "rally0",
                }
            }
        }

        nodes_info = {
            "nodes": {
                "FCFjozkeTiOpN-SI88YEcg": {
                    "name": "rally0",
                    "os": {
                        "name": "Mac OS X",
                        "version": "10.11.4",
                        "available_processors": 8
                    },
                    "jvm": {
                        "version": "1.8.0_74",
                        "vm_vendor": "Oracle Corporation"
                    }
                }
            }
        }
        cluster_info = {
            "version":
                {
                    "build_hash": "253032b",
                    "number": "5.0.0"

                }
        }
        client = Client(cluster=SubClient(nodes_stats), nodes=SubClient(nodes_info), info=cluster_info)
        metrics_store = metrics.EsMetricsStore(self.cfg)
        env_device = telemetry.ExternalEnvironmentInfo(self.cfg, client, metrics_store)
        t = telemetry.Telemetry(self.cfg, devices=[env_device])
        t.attach_to_cluster(cluster.Cluster([], t))

        calls = [
            mock.call(metrics.MetaInfoScope.cluster, None, "source_revision", "253032b"),
            mock.call(metrics.MetaInfoScope.cluster, None, "distribution_version", "5.0.0"),
            mock.call(metrics.MetaInfoScope.node, "rally0", "node_name", "rally0"),
            mock.call(metrics.MetaInfoScope.node, "rally0", "host_name", "unknown"),
            mock.call(metrics.MetaInfoScope.node, "rally0", "os_name", "Mac OS X"),
            mock.call(metrics.MetaInfoScope.node, "rally0", "os_version", "10.11.4"),
            mock.call(metrics.MetaInfoScope.node, "rally0", "cpu_logical_cores", 8),
            mock.call(metrics.MetaInfoScope.node, "rally0", "jvm_vendor", "Oracle Corporation"),
            mock.call(metrics.MetaInfoScope.node, "rally0", "jvm_version", "1.8.0_74")
        ]
        metrics_store_add_meta_info.assert_has_calls(calls)


class NodeStatsTests(TestCase):
    @mock.patch("esrally.metrics.EsMetricsStore.put_value_cluster_level")
    @mock.patch("esrally.metrics.EsMetricsStore.put_value_node_level")
    def test_stores_only_diff_of_gc_times(self, metrics_store_node_level, metrics_store_cluster_level):
        nodes_stats_at_start = {
            "nodes": {
                "FCFjozkeTiOpN-SI88YEcg": {
                    "name": "rally0",
                    "host": "127.0.0.1",
                    "jvm": {
                        "gc": {
                            "collectors": {
                                "old": {
                                    "collection_time_in_millis": 1000
                                },
                                "young": {
                                    "collection_time_in_millis": 500
                                }
                            }
                        }
                    }
                }
            }
        }

        client = Client(nodes=SubClient(nodes_stats_at_start))
        cfg = create_config()

        metrics_store = metrics.EsMetricsStore(cfg)
        device = telemetry.NodeStats(cfg, client, metrics_store)
        t = telemetry.Telemetry(cfg, devices=[device])
        t.on_benchmark_start()
        # now we'd need to change the node stats response
        nodes_stats_at_end = {
            "nodes": {
                "FCFjozkeTiOpN-SI88YEcg": {
                    "name": "rally0",
                    "host": "127.0.0.1",
                    "jvm": {
                        "gc": {
                            "collectors": {
                                "old": {
                                    "collection_time_in_millis": 2500
                                },
                                "young": {
                                    "collection_time_in_millis": 1200
                                }
                            }
                        }
                    }
                }
            }
        }
        client.nodes = SubClient(nodes_stats_at_end)
        t.on_benchmark_stop()

        metrics_store_node_level.assert_has_calls([
            mock.call("rally0", "node_young_gen_gc_time", 700, "ms"),
            mock.call("rally0", "node_old_gen_gc_time", 1500, "ms")
        ])

        metrics_store_cluster_level.assert_has_calls([
            mock.call("node_total_young_gen_gc_time", 700, "ms"),
            mock.call("node_total_old_gen_gc_time", 1500, "ms")
        ])


class IndexStatsTests(TestCase):
    @mock.patch("esrally.metrics.EsMetricsStore.put_value_cluster_level")
    @mock.patch("esrally.metrics.EsMetricsStore.put_count_cluster_level")
    def test_stores_available_index_stats(self, metrics_store_cluster_count, metrics_store_cluster_value):
        indices_stats = {
            "_all": {
                "primaries": {
                    "segments": {
                        "count": 5,
                        "memory_in_bytes": 2048,
                        "stored_fields_memory_in_bytes": 1024,
                        "doc_values_memory_in_bytes": 128,
                        "terms_memory_in_bytes": 256,
                        "points_memory_in_bytes": 512,
                        "file_sizes": {
                            "dii": { "size_in_bytes":      8552, "description": "Points" },
                            "doc": { "size_in_bytes": 236429758, "description": "Frequencies" },
                            "fdx": { "size_in_bytes":    636858, "description": "Field Index" },
                            "dim": { "size_in_bytes": 199771717, "description": "Points" },
                            "fdt": { "size_in_bytes": 812786379, "description": "Field Data" },
                            "fnm": { "size_in_bytes":    487464, "description": "Fields" },
                            "dvd": { "size_in_bytes": 692513616, "description": "DocValues" },
                            "dvm": { "size_in_bytes":    197706, "description": "DocValues" },
                            "tip": { "size_in_bytes":  11887500, "description": "Term Index" },
                            "tim": { "size_in_bytes": 658631045, "description": "Term Dictionary" },
                            "si":  { "size_in_bytes":     5736, "description": "Segment Info" },
                            "nvd": { "size_in_bytes": 94717780, "description": "Norms" },
                            "nvm": { "size_in_bytes":    18834, "description": "Norms" },
                            "pos": { "size_in_bytes": 51762724, "description": "Positions" }
                        }
                    },
                    "merges": {
                        "total_time_in_millis": 300,
                        "total_throttled_time_in_millis": 120
                    },
                    "indexing": {
                        "index_time_in_millis": 2000
                    },
                    "refresh": {
                        "total_time_in_millis": 200
                    },
                    "flush": {
                        "total_time_in_millis": 100
                    }
                }
            }
        }

        client = Client(indices=SubClient(indices_stats))
        cfg = create_config()

        metrics_store = metrics.EsMetricsStore(cfg)
        device = telemetry.IndexStats(cfg, client, metrics_store)
        t = telemetry.Telemetry(cfg, devices=[device])
        t.on_benchmark_start()
        t.on_benchmark_stop()

        metrics_store_cluster_count.assert_has_calls([
            mock.call("segments_count", 5)
        ])
        metrics_store_cluster_value.assert_has_calls([
            mock.call("segments_memory_in_bytes", 2048, "byte"),
            mock.call("segments_doc_values_memory_in_bytes", 128, "byte"),
            mock.call("segments_stored_fields_memory_in_bytes", 1024, "byte"),
            mock.call("segments_terms_memory_in_bytes", 256, "byte"),
            # we don't have norms, so nothing should have been called
            mock.call("segments_points_memory_in_bytes", 512, "byte"),
            mock.call("merges_total_time", 300, "ms"),
            mock.call("merges_total_throttled_time", 120, "ms"),
            mock.call("indexing_total_time", 2000, "ms"),
            mock.call("refresh_total_time", 200, "ms"),
            mock.call("flush_total_time", 100, "ms"),
            mock.call("dii_size_in_bytes", 8552, "byte"),
            mock.call("doc_size_in_bytes", 236429758, "byte"),
            mock.call("fdx_size_in_bytes", 636858, "byte"),
            mock.call("dim_size_in_bytes", 199771717, "byte"),
            mock.call("fdt_size_in_bytes", 812786379, "byte"),
            mock.call("fnm_size_in_bytes", 487464, "byte"),
            mock.call("dvd_size_in_bytes", 692513616, "byte"),
            mock.call("dvm_size_in_bytes", 197706, "byte"),
            mock.call("tip_size_in_bytes", 11887500, "byte"),
            mock.call("tim_size_in_bytes", 658631045, "byte"),
            mock.call("si_size_in_bytes", 5736, "byte"),
            mock.call("nvd_size_in_bytes", 94717780, "byte"),
            mock.call("nvm_size_in_bytes", 18834, "byte"),
            mock.call("pos_size_in_bytes", 51762724, "byte"),
        ])


class IndexSizeTests(TestCase):
    @mock.patch("esrally.utils.io.get_size")
    @mock.patch("esrally.metrics.EsMetricsStore.put_count_cluster_level")
    @mock.patch("esrally.utils.process.run_subprocess_with_logging")
    def test_stores_index_size_for_data_path(self, run_subprocess, metrics_store_cluster_count, get_size):
        get_size.return_value = 2048

        cfg = create_config()
        cfg.add(config.Scope.benchmark, "provisioning", "local.data.paths", ["/var/elasticsearch/data"])

        metrics_store = metrics.EsMetricsStore(cfg)
        device = telemetry.IndexSize(cfg, metrics_store)
        t = telemetry.Telemetry(cfg, devices=[device])
        t.on_benchmark_start()
        t.on_benchmark_stop()
        t.detach_from_cluster(None)

        metrics_store_cluster_count.assert_has_calls([
            mock.call("final_index_size_bytes", 2048, "byte")
        ])

        run_subprocess.assert_has_calls([
            mock.call("find /var/elasticsearch/data -ls", header="index files:")
        ])

    @mock.patch("esrally.utils.io.get_size")
    @mock.patch("esrally.metrics.EsMetricsStore.put_count_cluster_level")
    @mock.patch("esrally.utils.process.run_subprocess_with_logging")
    def test_stores_nothing_if_no_data_path(self, run_subprocess, metrics_store_cluster_count, get_size):
        get_size.return_value = 2048

        cfg = create_config()
        # no data path!

        metrics_store = metrics.EsMetricsStore(cfg)
        device = telemetry.IndexSize(cfg, metrics_store)
        t = telemetry.Telemetry(cfg, devices=[device])
        t.on_benchmark_start()
        t.on_benchmark_stop()
        t.detach_from_cluster(None)

        run_subprocess.assert_not_called()
        metrics_store_cluster_count.assert_not_called()
        get_size.assert_not_called()
