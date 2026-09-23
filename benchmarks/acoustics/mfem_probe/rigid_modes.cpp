#include "mfem.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

using Matrix = std::vector<double>;

static double &at(Matrix &a, int n, int i, int j)
{
   return a[static_cast<size_t>(i) * n + j];
}

static double atc(const Matrix &a, int n, int i, int j)
{
   return a[static_cast<size_t>(i) * n + j];
}

static Matrix Multiply(const Matrix &a, const Matrix &b, int n)
{
   Matrix c(static_cast<size_t>(n) * n, 0.0);
   for (int i = 0; i < n; ++i)
   {
      for (int k = 0; k < n; ++k)
      {
         const double aik = atc(a, n, i, k);
         if (aik == 0.0) { continue; }
         for (int j = 0; j < n; ++j)
         {
            at(c, n, i, j) += aik * atc(b, n, k, j);
         }
      }
   }
   return c;
}

static Matrix Transpose(const Matrix &a, int n)
{
   Matrix t(static_cast<size_t>(n) * n);
   for (int i = 0; i < n; ++i)
   {
      for (int j = 0; j < n; ++j)
      {
         at(t, n, j, i) = atc(a, n, i, j);
      }
   }
   return t;
}

static Matrix CholeskyLower(const Matrix &m, int n)
{
   Matrix l(static_cast<size_t>(n) * n, 0.0);
   for (int i = 0; i < n; ++i)
   {
      for (int j = 0; j <= i; ++j)
      {
         double sum = atc(m, n, i, j);
         for (int k = 0; k < j; ++k)
         {
            sum -= atc(l, n, i, k) * atc(l, n, j, k);
         }

         if (i == j)
         {
            if (!(sum > 0.0))
            {
               throw std::runtime_error("mass matrix is not positive definite");
            }
            at(l, n, i, j) = std::sqrt(sum);
         }
         else
         {
            at(l, n, i, j) = sum / atc(l, n, j, j);
         }
      }
   }
   return l;
}

static Matrix InvertLower(const Matrix &l, int n)
{
   Matrix inv(static_cast<size_t>(n) * n, 0.0);
   for (int col = 0; col < n; ++col)
   {
      for (int i = 0; i < n; ++i)
      {
         double rhs = (i == col) ? 1.0 : 0.0;
         for (int k = 0; k < i; ++k)
         {
            rhs -= atc(l, n, i, k) * atc(inv, n, k, col);
         }
         at(inv, n, i, col) = rhs / atc(l, n, i, i);
      }
   }
   return inv;
}

static std::vector<double> JacobiEigenvalues(Matrix a, int n, int &sweeps, double &max_offdiag)
{
   const int max_sweeps = 80;
   const double tol = 1e-12;
   sweeps = 0;
   max_offdiag = std::numeric_limits<double>::infinity();

   for (int sweep = 0; sweep < max_sweeps; ++sweep)
   {
      max_offdiag = 0.0;
      for (int p = 0; p < n - 1; ++p)
      {
         for (int q = p + 1; q < n; ++q)
         {
            const double apq = atc(a, n, p, q);
            max_offdiag = std::max(max_offdiag, std::abs(apq));
            if (std::abs(apq) < tol) { continue; }

            const double app = atc(a, n, p, p);
            const double aqq = atc(a, n, q, q);
            const double tau = (aqq - app) / (2.0 * apq);
            const double t = (tau >= 0.0)
                           ? 1.0 / (tau + std::sqrt(1.0 + tau * tau))
                           : -1.0 / (-tau + std::sqrt(1.0 + tau * tau));
            const double c = 1.0 / std::sqrt(1.0 + t * t);
            const double s = t * c;

            for (int k = 0; k < n; ++k)
            {
               if (k == p || k == q) { continue; }
               const double akp = atc(a, n, k, p);
               const double akq = atc(a, n, k, q);
               const double new_kp = c * akp - s * akq;
               const double new_kq = s * akp + c * akq;
               at(a, n, k, p) = at(a, n, p, k) = new_kp;
               at(a, n, k, q) = at(a, n, q, k) = new_kq;
            }

            at(a, n, p, p) = app - t * apq;
            at(a, n, q, q) = aqq + t * apq;
            at(a, n, p, q) = at(a, n, q, p) = 0.0;
         }
      }

      sweeps = sweep + 1;
      if (max_offdiag < tol) { break; }
   }

   std::vector<double> evals(n);
   for (int i = 0; i < n; ++i) { evals[i] = atc(a, n, i, i); }
   std::sort(evals.begin(), evals.end());
   return evals;
}

struct OrderResult
{
   int order = 0;
   int ndofs = 0;
   double assemble_s = 0.0;
   double eigensolve_s = 0.0;
   int jacobi_sweeps = 0;
   double jacobi_max_offdiag = 0.0;
   std::vector<double> frequencies_hz;
};

static OrderResult SolveOrder(int order, double lx, double ly, double lz, double sound_speed)
{
   using clock = std::chrono::steady_clock;

   mfem::Mesh mesh = mfem::Mesh::MakeCartesian3D(
      1, 1, 1, mfem::Element::HEXAHEDRON, lx, ly, lz, false);
   mfem::H1_FECollection fec(order, 3);
   mfem::FiniteElementSpace fes(&mesh, &fec);
   const int n = fes.GetTrueVSize();

   const auto assemble_start = clock::now();

   mfem::BilinearForm stiffness(&fes);
   stiffness.AddDomainIntegrator(new mfem::DiffusionIntegrator);
   stiffness.Assemble();
   stiffness.Finalize();

   mfem::BilinearForm mass(&fes);
   mass.AddDomainIntegrator(new mfem::MassIntegrator);
   mass.Assemble();
   mass.Finalize();

   Matrix a(static_cast<size_t>(n) * n, 0.0);
   Matrix m(static_cast<size_t>(n) * n, 0.0);
   mfem::Vector basis(n), result(n);
   basis = 0.0;
   for (int col = 0; col < n; ++col)
   {
      basis[col] = 1.0;
      stiffness.Mult(basis, result);
      for (int row = 0; row < n; ++row) { at(a, n, row, col) = result[row]; }
      mass.Mult(basis, result);
      for (int row = 0; row < n; ++row) { at(m, n, row, col) = result[row]; }
      basis[col] = 0.0;
   }

   const auto assemble_end = clock::now();

   const auto eigen_start = clock::now();
   Matrix l = CholeskyLower(m, n);
   Matrix linv = InvertLower(l, n);
   Matrix temp = Multiply(linv, a, n);
   Matrix transformed = Multiply(temp, Transpose(linv, n), n);

   // Remove tiny asymmetry introduced by finite-precision dense operations.
   for (int i = 0; i < n; ++i)
   {
      for (int j = i + 1; j < n; ++j)
      {
         const double avg = 0.5 * (atc(transformed, n, i, j) + atc(transformed, n, j, i));
         at(transformed, n, i, j) = at(transformed, n, j, i) = avg;
      }
   }

   int sweeps = 0;
   double max_offdiag = 0.0;
   std::vector<double> evals = JacobiEigenvalues(
      std::move(transformed), n, sweeps, max_offdiag);
   const auto eigen_end = clock::now();

   std::vector<double> frequencies;
   frequencies.reserve(16);
   constexpr double pi = 3.141592653589793238462643383279502884;
   for (double lambda : evals)
   {
      if (lambda <= 1e-8) { continue; } // Skip the Neumann constant mode.
      if (lambda < 0.0) { continue; }
      frequencies.push_back(sound_speed / (2.0 * pi) * std::sqrt(lambda));
      if (frequencies.size() >= 16) { break; }
   }

   if (frequencies.size() < 5)
   {
      throw std::runtime_error("MFEM probe produced fewer than five non-zero modes");
   }

   OrderResult out;
   out.order = order;
   out.ndofs = n;
   out.assemble_s = std::chrono::duration<double>(assemble_end - assemble_start).count();
   out.eigensolve_s = std::chrono::duration<double>(eigen_end - eigen_start).count();
   out.jacobi_sweeps = sweeps;
   out.jacobi_max_offdiag = max_offdiag;
   out.frequencies_hz = std::move(frequencies);
   return out;
}

int main(int argc, char *argv[])
{
   double lx = 4.0, ly = 5.0, lz = 2.5, sound_speed = 343.0;
   int order_min = 2, order_max = 5;
   std::string output;

   for (int i = 1; i < argc; ++i)
   {
      const std::string arg = argv[i];
      auto require_value = [&](const char *name) -> std::string
      {
         if (i + 1 >= argc) { throw std::runtime_error(std::string("missing value for ") + name); }
         return argv[++i];
      };

      if (arg == "--lx") { lx = std::stod(require_value("--lx")); }
      else if (arg == "--ly") { ly = std::stod(require_value("--ly")); }
      else if (arg == "--lz") { lz = std::stod(require_value("--lz")); }
      else if (arg == "--sound-speed") { sound_speed = std::stod(require_value("--sound-speed")); }
      else if (arg == "--order-min") { order_min = std::stoi(require_value("--order-min")); }
      else if (arg == "--order-max") { order_max = std::stoi(require_value("--order-max")); }
      else if (arg == "--output") { output = require_value("--output"); }
      else { throw std::runtime_error("unknown argument: " + arg); }
   }

   if (output.empty()) { throw std::runtime_error("--output is required"); }
   if (!(lx > 0.0 && ly > 0.0 && lz > 0.0 && sound_speed > 0.0))
   {
      throw std::runtime_error("room dimensions and sound speed must be positive");
   }
   if (order_min < 1 || order_max < order_min || order_max > 8)
   {
      throw std::runtime_error("invalid polynomial order range");
   }

   std::vector<OrderResult> results;
   for (int order = order_min; order <= order_max; ++order)
   {
      results.push_back(SolveOrder(order, lx, ly, lz, sound_speed));
   }

   std::ofstream os(output, std::ios::binary);
   if (!os) { throw std::runtime_error("cannot open output file: " + output); }
   os << std::setprecision(17);
   os << "{\n";
   os << "  \"schema_version\": \"r100b-mfem-rigid-modes-raw-1\",\n";
   os << "  \"mfem_version\": \"" << MFEM_VERSION_STRING << "\",\n";
   os << "  \"room_m\": [" << lx << ", " << ly << ", " << lz << "],\n";
   os << "  \"sound_speed_m_s\": " << sound_speed << ",\n";
   os << "  \"orders\": [\n";
   for (size_t ri = 0; ri < results.size(); ++ri)
   {
      const auto &r = results[ri];
      os << "    {\n";
      os << "      \"order\": " << r.order << ",\n";
      os << "      \"ndofs\": " << r.ndofs << ",\n";
      os << "      \"assemble_s\": " << r.assemble_s << ",\n";
      os << "      \"eigensolve_s\": " << r.eigensolve_s << ",\n";
      os << "      \"jacobi_sweeps\": " << r.jacobi_sweeps << ",\n";
      os << "      \"jacobi_max_offdiag\": " << r.jacobi_max_offdiag << ",\n";
      os << "      \"frequencies_hz\": [";
      for (size_t i = 0; i < r.frequencies_hz.size(); ++i)
      {
         if (i) { os << ", "; }
         os << r.frequencies_hz[i];
      }
      os << "]\n";
      os << "    }" << (ri + 1 == results.size() ? "" : ",") << "\n";
   }
   os << "  ]\n";
   os << "}\n";
   return 0;
}
