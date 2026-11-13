#if defined(FORGE_PROBE_COBALT)
#include <boost/asio/io_context.hpp>
#include <boost/cobalt/io/ssl.hpp>

int main()
{
    boost::asio::io_context io;
    boost::cobalt::io::ssl::context tls(boost::cobalt::io::ssl::context::tls_client);
    boost::cobalt::io::ssl::stream stream(tls, io.get_executor());
    return stream.secure() ? 1 : 0;
}
#elif defined(FORGE_PROBE_NNG)
#include <nng/nng.h>

int main()
{
    return nng_version()[0] != '\0' ? 0 : 1;
}
#elif defined(FORGE_PROBE_GRPC)
#include <grpcpp/security/credentials.h>

int main()
{
    return grpc::InsecureChannelCredentials() ? 0 : 1;
}
#elif defined(FORGE_PROBE_HPX)
#include <hpx/version.hpp>

int main()
{
    return hpx::full_version() != 0 ? 0 : 1;
}
#elif defined(FORGE_PROBE_FOLLY)
#include <folly/Demangle.h>

#include <typeinfo>

int main()
{
    return folly::demangle(typeid(int)).empty() ? 1 : 0;
}
#else
#error "Select a native consumer probe"
#endif
