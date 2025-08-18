#include <unistd.h>
#include <string>
#include <sstream>

#include <Panorama/trace.h>

struct Options
{
    std::string ConfigFile;
    int32_t Port;

    std::string ToString()
    {
        std::stringstream toString;
        toString << "\nConfigFile: " << ConfigFile << "\n";
        toString << "Port: " << Port;
        return toString.str();
    }
};

void PrintUsage(std::string name)
{
    std::stringstream usageMessage;
    usageMessage << "Usage: " << name << " <OPTION>\n";
    usageMessage << "-c\t" << "[REQUIRED] Path to application graph file\n";
    usageMessage << "-p\t" << "[OPTIONAL] Port (default is 8089)\n";
    usageMessage << "-l\t" << "[OPTIONAL] Log Level: (0 = Error, 1 = Warning, 2 = Information, 3 = Verbose).  Default is 2\n";
    usageMessage << "-h\t" << "[OPTIONAL] Print this message\n";

    fprintf(stderr, "%s", usageMessage.str().c_str());
}

Options GetOpts(int argc, char* argv[])
{
    Options options;
    options.ConfigFile = "";
    options.Port = 8089;

    int opt;
    while ((opt = getopt(argc, argv, "c:p:l:t")) != -1) 
    {
        switch (opt) {
        case 'c':
            options.ConfigFile = optarg;
            break;
        case 'p':
            options.Port = atoi(optarg);
            break;
        case 'l':
            Panorama::Tracer::SetTraceLevel(static_cast<Panorama::TraceLevel>(atoi(optarg)));
            break;
        default:
            PrintUsage(argv[0]);
            exit(1);
        }
    }

    if(options.ConfigFile.empty())
    {
        TraceError("application graph file not defined");
        PrintUsage(argv[0]);
        exit(1);
    }

    return options;
}