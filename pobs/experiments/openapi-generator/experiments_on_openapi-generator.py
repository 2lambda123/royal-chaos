#!/usr/bin/python
# -*- coding:utf-8 -*-

# build the application image first
  # git clone https://github.com/OpenAPITools/openapi-generator.git
  # git checkout f955966b09778bcbca891ff3d3ea4d063f16e554
  # cd modules/openapi-generator-online
  # mvn package -DskipTests=true
  # python base_image_generator.py -f /path/to/modules/openapi-generator-online/Dockerfile -o /path/to/modules/openapi-generator-online --build
  # docker build -t royalchaos/openapi-generator-online-pobs -f Dockerfile-pobs-application .

import os, time, re, datetime, json, csv, tempfile, subprocess, requests
import logging

import numpy as np
import pandas as pd
from causalimpact import CausalImpact

# return (headers, rows)
def read_from_csv(path):
    with open(path) as f:
        f_csv = csv.DictReader(f)
        return f_csv.fieldnames, list(f_csv)

def write_to_csv(path, headers, rows):
    with open(path, 'w', newline='') as file:
        f_csv = csv.DictWriter(file, headers)
        f_csv.writeheader()
        f_csv.writerows(rows)

def write_to_json(path, data):
    with open(path, 'w', newline='') as file:
        file.write(json.dumps(data, indent=4))

def workload_generator(duration):
    t_end = time.time() + 60 * duration
    cmd_workload = "curl -i --connect-timeout 2 -m 2 -X POST --header 'Content-Type: application/json' --header 'Accept: application/json' -d '{\"openAPIUrl\": \"https://raw.githubusercontent.com/openapitools/openapi-generator/master/modules/openapi-generator/src/test/resources/2_0/petstore.yaml\"}' 'http://localhost:8080/api/gen/clients/ruby' > /dev/null 2>&1"

    while time.time() < t_end:
        os.system(cmd_workload)
        time.sleep(0.01)

def query_glowroot(metric_name):
    # todo
    return

def causal_impact_analysis(ori_data, when_fi_started):
    x = list()
    y = list()
    post_period_index = 0
    for point in ori_data:
        x.append(point[0])
        y.append(point[1])
        if post_period_index == 0 and when_fi_started <= point[0]:
            post_period_index = ori_data.index(point)

    data_frame = pd.DataFrame({"timestamp": pd.to_datetime(x, unit="ms"), "y": y})
    data_frame = data_frame.set_index("timestamp")
    pre_period = [pd.to_datetime(ori_data[0][0], unit="ms"), pd.to_datetime(ori_data[post_period_index-1][0], unit="ms")]
    post_period = [pd.to_datetime(ori_data[post_period_index][0], unit="ms"), pd.to_datetime(ori_data[-1][0], unit="ms")]

    causal_impact = CausalImpact(data_frame, pre_period, post_period, prior_level_sd = 0.1)

    p = -1 # Posterior tail-area probability
    prob = -1 # Posterior prob. of a causal effect
    pattern = re.compile(r'Posterior tail-area probability p: (0\.\d+|[1-9]\d*\.\d+)\sPosterior prob. of a causal effect: (0\.\d+|[1-9]\d*\.\d+)%')
    match = pattern.search(causal_impact.summary())
    p = float(match.group(1))
    prob = float(match.group(2))
    summary = causal_impact.summary()
    report = causal_impact.summary(output='report')
    # causal_impact.plot()

    return summary, report, p, prob

def main():
    # load perturbation points list
    perturbation_point_csv = "./perturbationPointsList.csv"
    tripleagent_config_csv = "./logs/perturbationPointsList.csv"
    cmd_start_container = 'docker run --rm -d -p 8080:8080 -p 4000:4000 -e "TRIPLEAGENT_FILTER=org/openapitools/codegen" -e "TRIPLEAGENT_LINENUMBER=0" -v $PWD/logs:/home/tripleagent/logs royalchaos/openapi-generator-online-pobs:latest'
    url_query = 'http://localhost:4000/backend/jvm/gauges?agent-rollup-id=&from=%d&to=%d&gauge-name=java.lang%%3Atype%%3DMemory%%3AHeapMemoryUsage.used'

    headers, points = read_from_csv(perturbation_point_csv)
    if "tail-area probability" not in headers: headers.extend(["tail-area probability", "prob. of a causal effect", "p fi", "prob. fi"])

    for point in points:
        for i in range(2):
            # start an application container
            os.system(cmd_start_container)
            time.sleep(10)

            # 8 mins common workload
            start_at = int(time.time() * 1000)
            workload_generator(8)

            # 2 mins common workload / fault workload
            if i == 1:
                point["countdown"] = -1
                point["mode"] = "throw_e"
                write_to_csv(tripleagent_config_csv, headers, [point])
            time.sleep(2)
            when_fi_started = int(time.time() * 1000)
            workload_generator(2)
            end_at = int(time.time() * 1000)
            time.sleep(1)

            # query Glowroot to get last 10 minuets record
            request = requests.session()
            response = request.get(url_query%(start_at, end_at))
            ori_data = json.loads(response.content)

            # persist the monitoring data
            if not os.path.isdir("./monitoring_data"): os.system("mkdir ./monitoring_data")
            monitoring_data = {"data": ori_data["dataSeries"][0]["data"], "when_fi_started": when_fi_started}
            write_to_json("monitoring_data/%s-%d.json"%(point["key"], i), monitoring_data)

            # calculate causal impact of this specific exception
            summary, report, p, prob = causal_impact_analysis(ori_data["dataSeries"][0]["data"], when_fi_started)
            if i == 1:
                point["p fi"] = p
                point["prob. fi"] = prob
                logging.info("FI execution, %s"%point["key"])
                logging.info(summary)
                logging.info(report)
            else:
                point["tail-area probability"] = p
                point["prob. of a causal effect"] = prob
                logging.info("Normal execution, %s"%point["key"])
                logging.info(summary)
                logging.info(report)

            # clean up
            os.system("docker stop $(docker ps -q)")
            os.system("rm logs/perturbationPointsList.csv")

            write_to_csv(perturbation_point_csv, headers, points)

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    main()
